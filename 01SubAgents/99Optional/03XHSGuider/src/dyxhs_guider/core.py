"""Sample selection and evidence validation; no network or model calls.

The sibling TitleTag package supplies count/date parsing, source normalization,
deduplication and platform-local engagement scores. This module adds stricter
Guider requirements and never invents a source URL or content observation.

Content evidence belongs to the sample, in ``content_evidence`` (or ``evidence``):
``{id, source: subtitle|visual|metadata, text, url?, artifact?, start?, end?}``.
Analyses reference these IDs through ``observed_evidence: [{evidence_id}]`` and
``hypotheses: [{claim, evidence_ids}]``. A metadata-only analysis is preliminary.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit, urlunsplit


_LEGACY_SRC = Path(__file__).resolve().parents[4] / "02DYXHSTitleTag/src"
if str(_LEGACY_SRC) not in sys.path:
    sys.path.insert(0, str(_LEGACY_SRC))
from dyxhs_title_tag.sources import normalize_reference  # noqa: E402
from dyxhs_title_tag.ranking import (  # noqa: E402
    deduplicate_samples, parse_count, parse_datetime, rank_samples,
)


PLATFORMS = ("douyin", "xiaohongshu")
_ALIASES = {"dy": "douyin", "抖音": "douyin", "xhs": "xiaohongshu", "小红书": "xiaohongshu", "rednote": "xiaohongshu"}
_METRICS = ("likes", "comments", "collects", "shares")
_EXTRA_FIELDS = (
    "author_name", "author", "source", "source_url", "source_file", "source_page",
    "evidence", "content_evidence", "media_access_status", "access_status",
    "video_path", "subtitle_path", "transcript", "link_status", "source_snapshot",
    "source_keywords", "collection_status", "content_hash", "media_sha256",
    "media_urls", "duration_ms",
    "actual_video_watched", "source_index_crawled_at", "metric_observation_type",
)


def _platform(value: Any) -> str:
    value = str(value or "").strip().lower()
    return _ALIASES.get(value, value)


def _row_dict(value: Any) -> dict | None:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return dict(value) if isinstance(value, Mapping) else None


def _extract_rows(payload: Any, inherited_platform: str | None = None) -> list[tuple[dict, str | None]]:
    if isinstance(payload, (list, tuple)):
        return [item for value in payload for item in _extract_rows(value, inherited_platform)]
    row = _row_dict(payload)
    if row is None:
        return []
    inherited_platform = row.get("platform") or inherited_platform
    for key in ("samples", "references", "feeds", "aweme_list", "items", "notes", "data"):
        if isinstance(row.get(key), (list, tuple, dict)):
            return _extract_rows(row[key], inherited_platform)
    if any(key in row for key in ("id", "aweme_id", "note_id", "noteId", "aweme_info")):
        return [(row, inherited_platform)]
    return [item for key in PLATFORMS if key in row for item in _extract_rows(row[key], key)]


def _public_url(value: Any, platform: str) -> str:
    """Validate exact platform domains and item links; drop session parameters."""
    try:
        parts = urlsplit(str(value or "").strip())
        host = (parts.hostname or "").lower()
        if parts.scheme.lower() not in {"http", "https"} or not host or parts.username or parts.password:
            return ""
        if parts.port not in (None, 80, 443):
            return ""
        domains = ("douyin.com",) if platform == "douyin" else ("xiaohongshu.com", "xhslink.com") if platform == "xiaohongshu" else ()
        if not any(host == domain or host.endswith("." + domain) for domain in domains):
            return ""
        path = unquote(parts.path).rstrip("/")
        is_short = host == "v.douyin.com" or host == "xhslink.com" or host.endswith(".xhslink.com")
        if is_short:
            if not path or path in {"/a", "/search", "/explore"}:
                return ""
        elif not re.fullmatch((r"/video/[A-Za-z0-9_-]+" if platform == "douyin" else r"/(?:explore|discovery/item)/[A-Za-z0-9_-]+"), path):
            return ""
        return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), "", ""))
    except (TypeError, ValueError):
        return ""


def _url_item_id(url: str) -> str | None:
    match = re.fullmatch(r"/(?:video|explore|discovery/item)/([A-Za-z0-9_-]+)", urlsplit(url).path)
    return match.group(1) if match else None


def normalize_rows(payload: Any, platform: str | None = None, source_type: str = "import") -> list[dict]:
    """Normalize ReferenceSample-compatible exports without manufacturing links.

    Invalid but identifiable rows remain visible with ``normalization_errors``;
    selection then reports why they were rejected. Unknown video type stays
    unknown, rather than inheriting the old Douyin default-video assumption.
    """
    if platform is not None and _platform(platform) not in PLATFORMS:
        raise ValueError("platform must be douyin or xiaohongshu")
    result = []
    for original, inherited in _extract_rows(payload, platform):
        raw = original.get("aweme_info", original)
        if not isinstance(raw, dict):
            continue
        card = raw.get("noteCard") or raw.get("note_card") or raw.get("note") or raw
        if not isinstance(card, dict):
            continue
        explicit = _platform(raw.get("platform") or inherited or platform)
        if platform is not None and explicit in PLATFORMS and explicit != _platform(platform):
            continue
        source_url = next((raw.get(key) or card.get(key) for key in ("url", "note_url", "aweme_url", "share_url") if raw.get(key) or card.get(key)), "")
        try:
            normalized = normalize_reference(original, platform=explicit or None, source_type=source_type)
        except (TypeError, ValueError):
            normalized = None
        if normalized is None:
            continue
        sample = normalized.as_dict()
        sample["source_type"] = original.get("source_type") or raw.get("source_type") or source_type
        selected_platform = sample["platform"]
        source_url = _public_url(source_url, selected_platform)
        errors = list(original.get("normalization_errors") or [])
        if not source_url:
            errors.append("invalid_source_url")
        elif _url_item_id(source_url) and _url_item_id(source_url) != sample["id"]:
            errors.append("source_id_mismatch")
        sample["url"] = source_url
        sample["sample_id"] = f"{selected_platform}:{sample['id']}"
        explicit_type = card.get("content_type") or card.get("type")
        if explicit_type is None and not card.get("video") and "aweme_type" not in card:
            sample["content_type"] = "unknown"
        for field in _EXTRA_FIELDS:
            value = original.get(field, card.get(field))
            if value is not None:
                sample[field] = deepcopy(value)
        if sample.get("source_url"):
            sample["source_url"] = _public_url(sample["source_url"], selected_platform)
        if errors:
            sample["normalization_errors"] = list(dict.fromkeys(errors))
        result.append(sample)
    return result


def _topics(sample: dict, keywords: Iterable[str] = ()) -> dict:
    """Filter only by explicitly supplied keywords; no author topic defaults."""
    text = " ".join(str(sample.get(field) or "") for field in ("title", "description", "caption"))
    text += " " + " ".join(str(tag) for tag in sample.get("tags") or [])
    for evidence in sample.get("content_evidence") or sample.get("evidence") or []:
        if isinstance(evidence, dict):
            text += " " + str(evidence.get("text") or "")
    requested = list(dict.fromkeys(str(word).strip() for word in keywords if str(word).strip()))
    matched = [word for word in requested if word.casefold() in text.casefold()]
    return {"eligible": not requested or bool(matched), "labels": matched,
            "matched_keywords": {"requested": matched}}


def _fingerprint(sample: dict) -> str:
    explicit = sample.get("media_sha256") or sample.get("content_hash")
    if explicit:
        return "hash:" + str(explicit)
    text = sample.get("caption") or sample.get("description") or sample.get("title") or ""
    text = re.sub(r"#[^\s#]+", "", str(text)).lower()
    text = re.sub(r"[^\w\u3400-\u9fff]", "", text)
    return "text:" + text if len(text) >= 12 else ""


def select_samples(samples: Iterable[dict], *, total: int = 50, quotas: Mapping[str, int] | None = None,
                   recent_days: int = 365, now: Any = None, min_likes: Any = None,
                   max_per_author: int = 3, topic_keywords: Iterable[str] = ()) -> dict:
    """Select independently ranked platform samples without transferring quotas.

    ``min_likes`` is a nonnegative number or a per-platform mapping. At least
    one *interaction* (likes/comments/collects/shares, not views) must be > 0.
    All unknown metrics remain None. Cross-platform matches are review
    candidates, never asserted copies or silently removed.
    """
    if type(total) is not int or total < 1 or type(recent_days) is not int or recent_days < 1:
        raise ValueError("total and recent_days must be positive integers")
    if type(max_per_author) is not int or max_per_author < 1:
        raise ValueError("max_per_author must be a positive integer")
    chosen_quotas = {"douyin": total // 2 + total % 2, "xiaohongshu": total // 2} if quotas is None else {_platform(key): value for key, value in quotas.items()}
    if set(chosen_quotas) - set(PLATFORMS) or any(type(value) is not int or value < 0 for value in chosen_quotas.values()):
        raise ValueError("quotas require known platforms and nonnegative integers")
    chosen_quotas = {platform: chosen_quotas.get(platform, 0) for platform in PLATFORMS}
    if sum(chosen_quotas.values()) != total:
        raise ValueError("quotas must sum to total")
    thresholds = {}
    for platform in PLATFORMS:
        threshold = min_likes.get(platform) if isinstance(min_likes, Mapping) else min_likes
        threshold = 0 if threshold is None else parse_count(threshold)
        if threshold is None:
            raise ValueError("min_likes must contain nonnegative counts")
        thresholds[platform] = threshold
    current = parse_datetime(now) if now is not None else datetime.now(timezone.utc)
    if current is None:
        raise ValueError("now must be a valid datetime")
    raw = list(samples)
    normalized = normalize_rows(raw)
    excluded: list[dict] = []
    eligible = []

    def reject(sample: dict, reason: str) -> None:
        excluded.append({key: sample.get(key) for key in ("sample_id", "platform", "id", "url")} | {"reason": reason})

    # A malformed duplicate must not replace a valid snapshot's source URL.
    valid_normalized = []
    for sample in normalized:
        if sample.get("normalization_errors"):
            reject(sample, sample["normalization_errors"][0])
        else:
            valid_normalized.append(sample)
    unique = deduplicate_samples(valid_normalized)
    for sample in unique:
        sample["sample_id"] = f"{sample['platform']}:{sample['id']}"
        published = parse_datetime(sample.get("published_at"), reference=sample.get("collected_at"))
        topics = _topics(sample, topic_keywords)
        errors = sample.get("normalization_errors") or []
        reason = errors[0] if errors else None
        if reason is None and sample.get("content_type") != "video":
            reason = "not_confirmed_video"
        elif reason is None and not _public_url(sample.get("url"), sample["platform"]):
            reason = "invalid_source_url"
        elif reason is None and published is None:
            reason = "unknown_published_at"
        elif reason is None and published > current:
            reason = "future_published_at"
        elif reason is None and published < current - timedelta(days=recent_days):
            reason = "outside_window"
        elif reason is None and not topics["eligible"]:
            reason = "unrelated_topic"
        elif reason is None and not any(sample["metrics"].get(key) is not None and sample["metrics"][key] > 0 for key in _METRICS):
            reason = "no_nonzero_interactions"
        elif reason is None and thresholds[sample["platform"]] > 0 and sample["metrics"].get("likes") is None:
            reason = "unknown_likes_for_threshold"
        elif reason is None and thresholds[sample["platform"]] > 0 and sample["metrics"]["likes"] < thresholds[sample["platform"]]:
            reason = "below_min_likes"
        if reason:
            reject(sample, reason)
            continue
        sample["topics"] = topics["labels"]
        sample["topic_evidence"] = topics["matched_keywords"]
        sample["published_at"] = published.isoformat()
        # Legacy relevance inspects title/description; include the real caption.
        rank_input = deepcopy(sample)
        rank_input["description"] = " ".join(filter(None, (sample.get("description"), sample.get("caption"))))
        eligible.append((sample, rank_input))

    tokens = list(dict.fromkeys(word for sample, _ in eligible for values in sample["topic_evidence"].values() for word in values))
    if not tokens:
        tokens = list(dict.fromkeys(str(row.get("title") or row.get("description") or row.get("caption") or "").strip() for _, row in eligible))
        tokens = [word for word in tokens if word]
    ranked = rank_samples([row for _, row in eligible], tokens, now=current, recent_days=recent_days,
                          limit=len(eligible), min_references=0)
    originals = {sample["sample_id"]: sample for sample, _ in eligible}
    groups = defaultdict(list)
    for row in ranked["ranked_samples"]:
        sample = originals[f"{row['platform']}:{row['id']}"]
        sample["ranking"] = row["ranking"]
        sample["ranking"]["heat_score"] = row["ranking"]["engagement"]
        sample["ranking"]["score_type"] = "platform_sample_engagement_heuristic"
        sample["ranking"]["metric_coverage"] = len(row["ranking"]["available_metrics"]) / len(_METRICS)
        groups[sample["platform"]].append(sample)
    selected = []
    counts = Counter()
    for platform in PLATFORMS:
        groups[platform].sort(key=lambda row: (row["ranking"]["heat_score"], row["ranking"]["freshness"], row["id"]), reverse=True)
        authors = Counter()
        for row in groups[platform]:
            author = row.get("author_id") or row.get("author_name")
            if not author and isinstance(row.get("author"), dict):
                author = row["author"].get("id") or row["author"].get("nickname") or row["author"].get("name")
            if author and authors[author] >= max_per_author:
                reject(row, "author_limit")
            elif counts[platform] >= chosen_quotas[platform]:
                reject(row, "platform_quota_filled")
            else:
                counts[platform] += 1
                if author:
                    authors[author] += 1
                row["ranking"]["platform_rank"] = counts[platform]
                row["selection_reason"] = "视频类型与日期明确；按本次显式关键词筛选，通过互动门槛后按平台内样本排序。"
                selected.append(row)
    fingerprints = defaultdict(list)
    for row in selected:
        fingerprint = _fingerprint(row)
        if fingerprint:
            fingerprints[fingerprint].append(row)
    duplicates = []
    for rows in fingerprints.values():
        if len({row["platform"] for row in rows}) > 1:
            ids = [row["sample_id"] for row in rows]
            duplicates.append({"sample_ids": ids, "reason": "matching_content_hash_or_text", "status": "needs_review"})
            for row in rows:
                row["cross_platform_duplicate_candidates"] = [sample_id for sample_id in ids if sample_id != row["sample_id"]]
    missing = {platform: max(0, chosen_quotas[platform] - counts[platform]) for platform in PLATFORMS}
    return {
        "status": "complete" if not any(missing.values()) else "partial", "samples": selected,
        "stats": {"input_count": len(raw), "normalized_count": len(normalized), "unique_count": len(unique),
                  "selected_count": len(selected), "target_count": total, "quotas": chosen_quotas,
                  "platform_counts": {platform: counts[platform] for platform in PLATFORMS},
                  "eligible_counts": {platform: len(groups[platform]) for platform in PLATFORMS},
                  "missing_by_platform": missing, "duplicate_count": len(valid_normalized) - len(unique),
                  "unidentifiable_count": len(raw) - len(normalized), "excluded_counts": dict(Counter(row["reason"] for row in excluded)),
                  "recent_days": recent_days, "min_likes": thresholds, "as_of": current.isoformat()},
        "excluded": excluded, "cross_platform_duplicate_candidates": duplicates,
        "limitations": ["仅覆盖已采集样本；跨平台原始互动与分数不可直接比较。", "缺失互动保持未知；单次快照无法证明增长、互动率或传播因果。"],
    }


def _analysis_rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [deepcopy(row) for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("analyses"), list):
            return _analysis_rows(payload["analyses"])
        if "sample_id" in payload or "id" in payload:
            return [deepcopy(payload)]
        return [dict(deepcopy(row), sample_id=key) for key, row in payload.items() if isinstance(row, dict)]
    return []


def _evidence_registry(sample: dict) -> tuple[dict, list[str]]:
    registry, errors = {}, []
    for evidence in sample.get("content_evidence") or sample.get("evidence") or []:
        if not isinstance(evidence, dict):
            errors.append("invalid_sample_evidence")
            continue
        evidence_id = evidence.get("id") or evidence.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id.strip() or evidence_id in registry:
            errors.append("missing_or_duplicate_evidence_id")
            continue
        source = evidence.get("source")
        text = evidence.get("text")
        if source not in {"metadata", "subtitle", "visual"} or not isinstance(text, str) or not text.strip():
            errors.append(f"invalid_evidence:{evidence_id}")
            continue
        artifact = evidence.get("artifact") or evidence.get("path")
        url = evidence.get("url") or evidence.get("source_url")
        if not url and not artifact:
            errors.append(f"missing_evidence_source:{evidence_id}")
            continue
        if url:
            public_url = _public_url(url, sample["platform"])
            expected = _public_url(sample.get("url"), sample["platform"])
            if not public_url or public_url != expected:
                errors.append(f"foreign_evidence_url:{evidence_id}")
                continue
        if artifact and not Path(str(artifact)).expanduser().is_file():
            errors.append(f"missing_evidence_artifact:{evidence_id}")
            continue
        invalid_time = False
        for field in ("start", "end", "timestamp_seconds"):
            if field in evidence:
                value = evidence[field]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    invalid_time = True
        if invalid_time or ("start" in evidence and "end" in evidence and evidence["end"] < evidence["start"]):
            errors.append(f"invalid_evidence_time:{evidence_id}")
            continue
        registry[evidence_id] = deepcopy(evidence) | {"id": evidence_id}
    return registry, errors


def validate_analyses(samples: Iterable[dict], analyses: Any) -> dict:
    """Validate coverage, identity and content-evidence references.

    Returns normalized analyses and errors instead of claiming that structured
    JSON proves its content. Metadata-only rows remain explicitly preliminary.
    No analysis may upgrade its evidence level without sample-side evidence.
    """
    sample_rows = [deepcopy(row) for row in samples]
    sample_map = {}
    errors = []
    for sample in sample_rows:
        key = f"{_platform(sample.get('platform'))}:{sample.get('id')}"
        public_url = _public_url(sample.get("url"), _platform(sample.get("platform")))
        if not sample.get("id") or not public_url or (_url_item_id(public_url) and _url_item_id(public_url) != str(sample["id"])):
            errors.append({"sample_id": key, "reason": "invalid_sample_identity_or_url"})
        if key in sample_map:
            errors.append({"sample_id": key, "reason": "duplicate_sample_id"})
        sample_map[key] = sample
    rows = _analysis_rows(analyses)
    validated, seen = [], set()
    complete_count = preliminary_count = 0
    for row in rows:
        key = row.get("sample_id") or f"{_platform(row.get('platform'))}:{row.get('id')}"
        if not isinstance(key, str):
            errors.append({"sample_id": None, "reason": "invalid_sample_id"})
            continue
        row_errors = []
        sample = sample_map.get(key)
        if sample is None:
            errors.append({"sample_id": key, "reason": "unknown_sample_id"})
            continue
        if key in seen:
            errors.append({"sample_id": key, "reason": "duplicate_analysis"})
            continue
        seen.add(key)
        if row.get("platform") is not None and _platform(row["platform"]) != _platform(sample.get("platform")):
            row_errors.append("platform_mismatch")
        if row.get("id") is not None and str(row["id"]) != str(sample["id"]):
            row_errors.append("id_mismatch")
        row.update(sample_id=key, platform=sample["platform"], id=str(sample["id"]))
        if row.get("url") and _public_url(row["url"], sample["platform"]) != _public_url(sample.get("url"), sample["platform"]):
            row_errors.append("analysis_url_mismatch")
        registry, evidence_errors = _evidence_registry(sample)
        row_errors.extend(evidence_errors)
        observed, observed_ids = [], set()
        supplied_observations = row.get("observed_evidence")
        if not isinstance(supplied_observations, list) or not supplied_observations:
            row_errors.append("missing_observed_evidence")
            supplied_observations = []
        for observation in supplied_observations:
            evidence_id = observation if isinstance(observation, str) else observation.get("evidence_id") or observation.get("id") if isinstance(observation, dict) else None
            evidence = registry.get(evidence_id) if isinstance(evidence_id, str) else None
            if evidence is None:
                row_errors.append(f"unknown_evidence_id:{evidence_id}")
                continue
            if isinstance(observation, dict) and any(field in observation and observation[field] != evidence.get(field) for field in ("text", "source", "url", "start", "end", "timestamp_seconds")):
                row_errors.append(f"evidence_content_mismatch:{evidence_id}")
                continue
            if evidence_id not in observed_ids:
                observed_ids.add(evidence_id)
                observed.append(evidence | {"evidence_id": evidence_id})
        hypotheses = row.get("hypotheses")
        if not isinstance(hypotheses, list) or not hypotheses:
            row_errors.append("missing_hypotheses")
            hypotheses = []
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, dict):
                row_errors.append("invalid_hypothesis")
                continue
            claim = hypothesis.get("claim") or hypothesis.get("text")
            evidence_ids = hypothesis.get("evidence_ids")
            if not isinstance(claim, str) or not claim.strip() or not isinstance(evidence_ids, list) or not evidence_ids or any(not isinstance(evidence_id, str) or evidence_id not in observed_ids for evidence_id in evidence_ids):
                row_errors.append("ungrounded_hypothesis")
        for field in ("content_summary", "transferable_idea"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                row_errors.append(f"missing_{field}")
        if not isinstance(row.get("limitations"), list) or not row["limitations"] or not all(isinstance(value, str) and value.strip() for value in row["limitations"]):
            row_errors.append("missing_limitations")
        confidence = row.get("confidence")
        if not ((isinstance(confidence, str) and confidence in {"low", "medium", "high", "低", "中", "高"}) or (not isinstance(confidence, bool) and isinstance(confidence, (int, float)) and math.isfinite(confidence) and 0 <= confidence <= 1)):
            row_errors.append("invalid_confidence")
        sources = {evidence["source"] for evidence in observed}
        content_sources = sources & {"subtitle", "visual"}
        declared = row.get("evidence_level")
        allowed = {"metadata", "subtitle", "visual", "subtitle_visual", "metadata+subtitle", "metadata+visual", "metadata+subtitle+visual"}
        if not isinstance(declared, str) or declared not in allowed:
            row_errors.append("invalid_evidence_level")
        elif ("visual" in declared and "visual" not in content_sources) or ("subtitle" in declared and "subtitle" not in content_sources):
            row_errors.append("overstated_evidence_level")
        actual_level = "subtitle_visual" if len(content_sources) == 2 else next(iter(content_sources)) if content_sources else "metadata"
        row["evidence_level"] = actual_level
        row["observed_evidence"] = observed
        row["validation_errors"] = list(dict.fromkeys(row_errors))
        if row_errors:
            row["validation_status"] = "invalid"
        elif not content_sources:
            row["validation_status"] = "preliminary"
            preliminary_count += 1
        else:
            row["validation_status"] = "complete"
            complete_count += 1
        errors.extend({"sample_id": key, "reason": reason} for reason in row["validation_errors"])
        validated.append(row)
    missing = sorted(set(sample_map) - seen)
    errors.extend({"sample_id": key, "reason": "missing_analysis"} for key in missing)
    return {"status": "complete" if sample_map and complete_count == len(sample_map) and not errors else "analysis_incomplete",
            "analyses": validated, "errors": errors,
            "stats": {"sample_count": len(sample_map), "analysis_count": len(validated), "complete_count": complete_count,
                      "preliminary_count": preliminary_count, "missing_count": len(missing), "error_count": len(errors)}}
