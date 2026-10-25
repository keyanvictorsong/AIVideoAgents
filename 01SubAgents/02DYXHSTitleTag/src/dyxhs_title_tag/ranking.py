"""Explainable platform-local sample ranking and evidence-backed hashtag selection.

Scores describe the imported sample pool, never a platform-wide popularity rank.
Unknown counts remain unknown; relative publication dates use the sample's actual
collection time. Missing publication dates cannot establish recency.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

UTC = timezone.utc
_PLATFORM_NAMES = {
    "dy": "douyin", "抖音": "douyin", "douyin": "douyin",
    "xhs": "xiaohongshu", "小红书": "xiaohongshu", "rednote": "xiaohongshu",
    "xiaohongshu": "xiaohongshu",
}
_WEIGHTS = {
    "douyin": {"likes": .35, "comments": .2, "collects": .15, "shares": .3},
    "xiaohongshu": {"likes": .35, "comments": .2, "collects": .4, "shares": .05},
}
# No author-specific topic vocabulary is bundled. Match supplied keywords directly.
_TAG_FAMILIES: tuple[set[str], ...] = ()


def parse_count(value: Any) -> float | None:
    """Parse visible Chinese/English count units, preserving absent values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) and value >= 0 else None
    raw = str(value).strip().replace(",", "").replace("，", "")
    if raw.lower() in {"", "-", "--", "null", "none", "nan", "未知", "暂无"}:
        return None
    match = re.search(r"(?<![\d.])(-?\d+(?:\.\d+)?)\s*([千万亿wkmbWKMB]?)", raw)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"千": 1e3, "万": 1e4, "亿": 1e8, "k": 1e3, "w": 1e4,
                  "m": 1e6, "b": 1e9}.get(match.group(2).lower(), 1)
    return number * multiplier if number >= 0 else None


def parse_datetime(value: Any, *, reference: Any = None) -> datetime | None:
    """Return UTC-aware time. Relative dates require an explicit collection time.

    Naive datetimes are interpreted as UTC. Date-only Chinese platform strings
    are interpreted in China time (UTC+8); ISO date-only values are UTC.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) or str(value).strip().isdigit():
        try:
            timestamp = float(value)
            if timestamp > 1e11:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, UTC)
        except (ValueError, OverflowError, OSError):
            return None
    else:
        raw = str(value).strip()
        ref = parse_datetime(reference) if reference is not None else None
        relative = re.fullmatch(r"(\d+)\s*(秒|分钟|小时|天|周)前", raw)
        if relative:
            if ref is None:
                return None
            count, unit = int(relative.group(1)), relative.group(2)
            seconds = {"秒": 1, "分钟": 60, "小时": 3600, "天": 86400, "周": 604800}
            return ref - timedelta(seconds=count * seconds[unit])
        if raw in {"刚刚", "今天", "昨天", "前天"}:
            return ref - timedelta(days={"刚刚": 0, "今天": 0, "昨天": 1, "前天": 2}[raw]) if ref else None
        if "年" in raw:
            raw = raw.replace("年", "-").replace("月", "-").replace("日", "").strip()
            if not re.search(r"(?:Z|[+-]\d\d:\d\d)$", raw):
                raw += "T00:00:00+08:00" if " " not in raw and "T" not in raw else "+08:00"
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _platform(value: Any) -> str:
    raw = str(value or "unknown").lower().strip()
    return _PLATFORM_NAMES.get(raw, raw)


def _url(value: Any) -> str:
    if not value:
        return ""
    try:
        parts = urlsplit(str(value).strip())
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return ""
        query = [(k, v) for k, v in parse_qsl(parts.query)
                 if not k.lower().startswith(("utm_", "xsec_"))
                 and k.lower() not in {"source", "share_from", "share_token", "share_app_id"}]
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(sorted(query)), ""))
    except ValueError:
        return ""


def _tags(sample: dict[str, Any]) -> list[str]:
    values = sample.get("tags") or []
    if isinstance(values, str):
        values = re.findall(r"#([\w\u3400-\u9fff]+)", values) or re.split(r"[,，;；\s]+", values)
    elif not isinstance(values, (list, tuple)):
        values = [values]
    candidates = []
    for item in values:
        if isinstance(item, dict):
            item = next((item.get(key) for key in ("name", "tag_name", "title", "text") if item.get(key)), "")
        if item:
            clean = re.sub(r"\[话题\]", "", str(item)).strip().strip("#").strip()
            if re.fullmatch(r"[\w\u3400-\u9fff]{1,40}", clean):
                candidates.append(clean)
    for field in ("title", "description"):
        candidates.extend(re.findall(r"#([\w\u3400-\u9fff]{1,40})", str(sample.get(field) or "")))
    return list(dict.fromkeys(candidates))


def deduplicate_samples(samples: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge connected ID/URL duplicates within each platform, never across it."""
    source = [dict(sample) for sample in samples]
    parent = list(range(len(source)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen: dict[tuple[str, str, str], int] = {}
    for i, sample in enumerate(source):
        platform = _platform(sample.get("platform"))
        sample["platform"] = platform
        for kind, value in (("id", str(sample.get("id") or "").strip()), ("url", _url(sample.get("url")))):
            if value:
                key = (platform, kind, value)
                if key in seen:
                    parent[root(i)] = root(seen[key])
                seen[key] = i
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for i, sample in enumerate(source):
        groups[root(i)].append(sample)
    result = []
    for group in groups.values():
        # The latest recorded snapshot wins; missing fields can be filled from
        # older snapshots. Do not synthesize a current collected_at value.
        group.sort(key=lambda s: parse_datetime(s.get("collected_at")) or datetime.min.replace(tzinfo=UTC), reverse=True)
        merged: dict[str, Any] = {}
        for sample in group:
            for key, value in sample.items():
                if merged.get(key) in (None, "", [], {}):
                    merged[key] = value
        merged["tags"] = list(dict.fromkeys(tag for sample in group for tag in _tags(sample)))
        merged["metrics"] = {}
        for key in ("likes", "comments", "collects", "shares", "views"):
            def count(s: dict) -> float | None:
                metrics = s.get("metrics") or {}
                return parse_count(metrics.get(key, metrics.get("favorites") if key == "collects" else None))
            merged["metrics"][key] = next((count(s) for s in group if count(s) is not None), None)
        merged["duplicate_count"] = len(group)
        result.append(merged)
    return result


def _tokens(topic_tokens: Iterable[str] | str) -> list[str]:
    if isinstance(topic_tokens, str):
        topic_tokens = re.split(r"[\s,，、;/]+", topic_tokens)
    return list(dict.fromkeys(str(token).strip().lower() for token in topic_tokens if str(token).strip()))


def _relevance(sample: dict[str, Any], tokens: list[str]) -> tuple[float, list[str]]:
    haystack = " ".join([str(sample.get("title") or ""), str(sample.get("description") or ""), " ".join(_tags(sample))]).lower()
    matches = [token for token in tokens if token in haystack]
    # One matching concrete topic can qualify a sample; more coverage raises
    # confidence. The search query alone cannot establish content relevance.
    return min(1.0, len(matches) / max(1, min(3, len(tokens)))), matches


def rank_samples(
    samples: Iterable[dict[str, Any]],
    topic_tokens: Iterable[str] | str,
    *,
    now: datetime | str | None = None,
    recent_days: int = 7,
    limit: int = 50,
    min_references: int = 50,
) -> dict[str, Any]:
    """Filter traceable recent samples, then rank each platform independently.

    ``limit`` and ``min_references`` are per platform. Counts with no visible
    metrics have a null engagement score, never an invented engagement rate.
    """
    if recent_days <= 0 or limit < 0 or min_references < 0:
        raise ValueError("recent_days must be positive; limits must be nonnegative")
    current = parse_datetime(now) if now is not None else datetime.now(UTC)
    if current is None:
        raise ValueError("now must be a valid datetime")
    tokens = _tokens(topic_tokens)
    source = list(samples)
    unique = deduplicate_samples(source)
    platform_stats: dict[str, dict[str, Any]] = {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in unique:
        platform = sample["platform"]
        stats = platform_stats.setdefault(platform, {"unique_count": 0, "eligible_count": 0, "selected_count": 0, "excluded": Counter()})
        stats["unique_count"] += 1
        published = parse_datetime(sample.get("published_at"), reference=sample.get("collected_at"))
        reason = None
        if sample.get("content_type") not in (None, "video"):
            reason = "not_confirmed_video"
        elif not _url(sample.get("url")):
            reason = "missing_source_url"
        elif published is None:
            reason = "unknown_published_at"
        elif published > current:
            reason = "future_published_at"
        elif published < current - timedelta(days=recent_days):
            reason = "outside_window"
        relevance, matched = _relevance(sample, tokens)
        if reason is None and not matched:
            reason = "unrelated"
        if reason is None and not any(sample["metrics"].get(key) is not None
                                      for key in _WEIGHTS.get(platform, _WEIGHTS["douyin"])):
            reason = "missing_engagement_metrics"
        if reason:
            stats["excluded"][reason] += 1
            continue
        sample["published_at_utc"] = published.isoformat()
        age_days = (current - published).total_seconds() / 86400
        sample["ranking"] = {"relevance": relevance, "matched_topic_tokens": matched,
                             "age_days": round(age_days, 4), "freshness": math.exp(-age_days / recent_days)}
        groups[platform].append(sample)
    ranked = []
    for platform, candidates in groups.items():
        weights = _WEIGHTS.get(platform, _WEIGHTS["douyin"])
        maxima = {key: max((math.log1p(s["metrics"][key]) for s in candidates if s["metrics"].get(key) is not None), default=0.0)
                  for key in weights}
        for sample in candidates:
            available = {key: weight for key, weight in weights.items() if sample["metrics"].get(key) is not None}
            weight_total = sum(available.values())
            engagement = sum(weight * (math.log1p(sample["metrics"][key]) / maxima[key] if maxima[key] else 0.0)
                             for key, weight in available.items()) / weight_total if weight_total else None
            ranking = sample["ranking"]
            ranking.update({"engagement": engagement, "available_metrics": list(available),
                            "score": round(.5 * ranking["relevance"] + .4 * (engagement or 0) + .1 * ranking["freshness"], 8),
                            "score_type": "sample_pool_heuristic"})
        candidates.sort(key=lambda s: (s["ranking"]["score"], s["published_at_utc"], str(s.get("id") or "")), reverse=True)
        for position, sample in enumerate(candidates[:limit], 1):
            sample["ranking"]["platform_rank"] = position
            ranked.append(sample)
        stats = platform_stats[platform]
        stats["eligible_count"] = len(candidates)
        stats["selected_count"] = min(limit, len(candidates))
    for stats in platform_stats.values():
        stats["excluded"] = dict(stats["excluded"])
        stats["status"] = "ok" if stats["selected_count"] >= min_references else "insufficient_samples"
        stats["missing_to_target"] = max(0, min_references - stats["selected_count"])
    sufficient = bool(platform_stats) and all(s["status"] == "ok" for s in platform_stats.values())
    return {"status": "ok" if sufficient else "insufficient_samples", "ranked_samples": ranked,
            "window_days": recent_days, "as_of": current.isoformat(), "target_per_platform": min_references,
            "stats": {"input_count": len(source), "unique_count": len(unique),
                      "duplicates_removed": len(source) - len(unique), "platforms": platform_stats},
            "limitations": ["Ranking covers the supplied sample pool, not the full platform.",
                            "A single metrics snapshot does not measure growth or engagement rate."]}


def _tag_matches(tag: str, tokens: list[str]) -> list[str]:
    lower = tag.lower()
    matched = [token for token in tokens if token in lower or (len(lower) > 1 and lower in token)]
    for family in _TAG_FAMILIES:
        if any(member.lower() in token for member in family for token in tokens):
            if any(member.lower() in lower for member in family):
                matched.extend(token for token in tokens if any(member.lower() in token for member in family))
    return list(dict.fromkeys(matched))


def select_tags(
    ranked_samples: Iterable[dict[str, Any]],
    topic_tokens: Iterable[str] | str,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    """Select real relevant XHS hashtags; never invent tags to reach ``limit``."""
    if limit < 0:
        raise ValueError("limit must be nonnegative")
    tokens = _tokens(topic_tokens)
    samples = deduplicate_samples(s for s in ranked_samples if _platform(s.get("platform")) == "xiaohongshu")
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    spellings: dict[str, str] = {}
    rejected: set[str] = set()
    for sample in samples:
        if not _url(sample.get("url")):
            continue
        for tag in _tags(sample):
            key = tag.lower()
            if not _tag_matches(tag, tokens):
                rejected.add(tag)
                continue
            spellings.setdefault(key, tag)
            evidence[key].append(sample)
    tags = []
    for key, references in evidence.items():
        authors = set()
        unknown = 0
        author_scores: dict[str, float] = {}
        dates = []
        for index, sample in enumerate(references):
            author = sample.get("author_id") or sample.get("author")
            if isinstance(author, dict):
                author = author.get("id") or author.get("user_id") or author.get("nickname") or author.get("name")
            if author:
                author_key = str(author)
                authors.add(author_key)
            else:
                unknown += 1
                author_key = f"unknown-sample-{index}"
            score = float((sample.get("ranking") or {}).get("score") or 0)
            author_scores[author_key] = max(author_scores.get(author_key, 0), score)
            if sample.get("published_at_utc") or sample.get("published_at"):
                dates.append(sample.get("published_at_utc") or sample.get("published_at"))
        # Frequency is capped per known author in the heat score. All distinct
        # note references remain visible so concentration is auditable.
        score = sum(1 + value for value in author_scores.values())
        tags.append({"tag": spellings[key], "score": round(score, 6),
                     "sample_count": len(references), "author_count": len(authors),
                     "unknown_author_sample_count": unknown,
                     "reference_ids": list(dict.fromkeys(str(s["id"]) for s in references if s.get("id"))),
                     "urls": list(dict.fromkeys(str(s["url"]) for s in references)),
                     "evidence_type": "sample", "matched_topic_tokens": _tag_matches(spellings[key], tokens),
                     "sample_published_dates": dates,
                     "reason": "Observed in relevant supplied samples; no official topic heat metric was available."})
    tags.sort(key=lambda tag: (tag["score"], tag["author_count"], tag["sample_count"], tag["tag"]), reverse=True)
    selected = tags[:limit]
    return {"status": "ok" if len(selected) >= limit else "insufficient_tag_evidence", "tags": selected,
            "target": limit, "missing_to_target": max(0, limit - len(selected)),
            "stats": {"xhs_sample_count": len(samples), "relevant_distinct_tags": len(tags),
                      "unrelated_tags_rejected": sorted(rejected)},
            "evidence_type": "sample"}


def analyze_platform_samples(
    samples: Iterable[dict[str, Any]],
    topic_tokens: Iterable[str] | str,
    *,
    now: datetime | str | None = None,
    min_references: int = 50,
    limit: int = 60,
) -> dict[str, Any]:
    """Use seven days first; expand only each insufficient platform to 30 days."""
    source = list(samples)
    tokens = _tokens(topic_tokens)
    current = parse_datetime(now) if now is not None else datetime.now(UTC)
    initial = rank_samples(source, tokens, now=current, recent_days=7, limit=limit, min_references=min_references)
    selected = []
    platform_results = {}
    for platform, stats in initial["stats"]["platforms"].items():
        platform_source = [s for s in source if _platform(s.get("platform")) == platform]
        result = (rank_samples(platform_source, tokens, now=current, recent_days=30, limit=limit, min_references=min_references)
                  if stats["status"] != "ok" else
                  rank_samples(platform_source, tokens, now=current, recent_days=7, limit=limit, min_references=min_references))
        platform_results[platform] = result
        selected.extend(result["ranked_samples"])
    tags = select_tags(selected, tokens)
    return {"status": "ok" if platform_results and all(r["status"] == "ok" for r in platform_results.values()) else "insufficient_samples",
            "platforms": platform_results, "ranked_samples": selected, "tag_analysis": tags,
            "as_of": current.isoformat() if current else None}
