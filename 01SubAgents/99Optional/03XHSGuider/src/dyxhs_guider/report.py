"""Render an evidence-first, self-contained Chinese video research report.

Only the Python standard library is required. Collected text is escaped before
it enters HTML, and the small offline script only filters/reorders existing DOM
nodes. No collected value is interpreted as JavaScript or HTML.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime
from html import escape
import json
import math
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


_PLATFORMS = {"douyin": "抖音", "xiaohongshu": "小红书"}
_STATUSES = {
    "complete": "已完成", "completed": "已完成", "success": "已采集",
    "ok": "已采集", "partial": "部分完成", "blocked": "访问受限",
    "failed": "采集失败", "error": "采集失败", "pending": "待采集",
    "not_started": "尚未开始", "skipped": "已跳过", "empty": "暂无结果",
    "login_required": "需要登录", "requires_login": "需要登录",
    "verification_required": "需要人工验证", "running": "采集中",
    "rate_limited": "验证或访问限制", "insufficient_samples": "样本不足",
    "network_timeout": "页面加载超时", "network_error": "网络错误",
    "service_unavailable": "服务不可用", "browser_unavailable": "浏览器不可用",
    "hydration_timeout": "搜索结果加载超时", "page_error": "页面读取失败",
    "browser_setting_required": "待开启浏览器读取",
}
_EVIDENCE_TYPES = {
    "metadata": "页面元数据", "metadata_only": "页面元数据", "title": "标题",
    "description": "正文", "caption": "字幕", "transcript": "转录文字",
    "subtitle": "字幕", "subtitles": "字幕", "frame": "视频画面",
    "frames": "视频画面", "keyframe": "关键画面", "keyframes": "关键画面",
    "visual": "视频画面", "video": "视频内容", "content": "视频内容",
    "comment": "评论样本", "comments": "评论样本", "metrics": "互动快照",
}
_CONTENT_TYPES = {
    "transcript", "caption", "subtitle", "subtitles", "frame", "frames",
    "keyframe", "keyframes", "visual", "video", "content", "ocr",
    "video_frame", "video_transcript", "video_content", "key_frames",
}


def _mapping(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "to_dict"):
        converted = value.to_dict()
        return converted if isinstance(converted, dict) else {}
    return {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "、".join(_text(item) for item in value if item is not None)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _h(value: Any) -> str:
    return escape(_text(value), quote=True)


def _display_time(value: Any) -> str:
    """Keep source timestamps in data; display aware dates in the user's zone."""
    raw = _text(value).strip()
    if not raw:
        return "时间未提供"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return raw
        local = parsed.astimezone(ZoneInfo("America/Los_Angeles"))
        return local.strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return raw


def _list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _safe_url(value: Any) -> str:
    """Allow absolute web links, dropping temporary XHS access tokens."""
    url = _text(value).strip()
    if not url or re.search(r"[\x00-\x20\x7f]", url):
        return ""
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return ""
        if parts.username is not None or parts.password is not None:
            return ""
        # Accessing .port also rejects malformed URLs instead of emitting them.
        _ = parts.port
        hostname = parts.hostname.lower()
        if hostname == "xiaohongshu.com" or hostname.endswith(".xiaohongshu.com"):
            query = [
                (key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
                if key.lower() not in {"xsec_token", "xsec_source"}
            ]
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        return url
    except (ValueError, TypeError):
        return ""


def _link(value: Any, label: str, class_name: str = "") -> str:
    url = _safe_url(value)
    if not url:
        return ""
    return f'<a class="{class_name}" href="{_h(url)}" target="_blank" rel="noopener noreferrer">{_h(label)}</a>'


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value) if math.isfinite(value) and value >= 0 else None
        except OverflowError:
            return None
    text = _text(value).replace(",", "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([万亿kKwW]?)\+?", text)
    if not match:
        return None
    multiplier = {"万": 10_000, "亿": 100_000_000, "k": 1_000, "w": 10_000}.get(match[2].lower(), 1)
    number = float(match[1]) * multiplier
    return number if math.isfinite(number) else None


def _metric(sample: dict, name: str) -> Any:
    metrics = _mapping(sample.get("metrics"))
    aliases = {
        "likes": ("likes", "liked_count", "digg_count", "like_count"),
        "comments": ("comments", "comment_count"),
        "favorites": ("favorites", "collects", "collect_count", "collected_count", "favorite_count"),
        "shares": ("shares", "share_count", "shared_count"),
        "views": ("views", "play_count", "view_count"),
    }
    for field in aliases[name]:
        if field in metrics and metrics[field] is not None:
            return metrics[field]
        if field in sample and sample[field] is not None:
            return sample[field]
    return None


def _format_count(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "—"
    if isinstance(value, str):
        return value
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.1f}"


def _topics(sample: dict) -> list[str]:
    for key in ("topics", "themes", "topic", "theme"):
        if sample.get(key):
            return list(dict.fromkeys(_text(item).strip() for item in _list(sample[key]) if _text(item).strip()))
    return ["主题待核验"]


def _resolved_evidence(sample: dict, analysis: dict) -> list[dict]:
    """Resolve analysis references to the original, sample-side observations."""
    registry = {}
    for value in _list(sample.get("content_evidence") or sample.get("evidence")):
        item = _mapping(value)
        eid = _text(item.get("id") or item.get("evidence_id"))
        if eid and eid not in registry:
            registry[eid] = item
    result = []
    for value in _list(analysis.get("observed_evidence")):
        item = _mapping(value)
        eid = _text(item.get("evidence_id") or item.get("id") or (value if isinstance(value, str) else ""))
        if eid in registry:
            item = dict(registry[eid])
        elif not item:
            item = {"id": eid, "text": "引用未找到；须补充对应的样本证据。"}
        if item:
            item = dict(item)
            item["id"] = eid or item.get("id")
            item["type"] = item.get("source") or item.get("type") or ""
            item["url"] = item.get("url") or item.get("source_url")
            result.append(item)
    return result


def _analysis_info(sample: dict) -> tuple[dict, str, str, str]:
    analysis = _mapping(sample.get("analysis"))
    validation = _text(analysis.get("validation_status"))
    if validation == "invalid":
        return analysis, "invalid", "分析核验未通过", "证据引用或分析字段未通过校验，不能计为完成"
    has_analysis = bool(analysis.get("content_summary") or analysis.get("hypotheses") or analysis.get("transferable_idea"))
    if not has_analysis:
        return analysis, "pending", "待分析", "尚未完成逐条分析"
    level = _text(analysis.get("evidence_level")).strip().lower()
    evidence = _resolved_evidence(sample, analysis)
    has_content = any(
        _text(item.get("type")).lower() in _CONTENT_TYPES and bool(item.get("text"))
        for item in evidence
    )
    if has_content and validation != "preliminary":
        has_comments = any(_text(item.get("type")).lower() in {"comment", "comments"} for item in evidence)
        label = "内容证据＋评论" if has_comments else "有视频内容证据"
        return analysis, "content", label, "已有字幕、转录或实际画面等内容证据"
    # A self-declared level alone does not establish that the video was viewed.
    if evidence or level in {"metadata", "metadata_only", "title", "description", "页面元数据"}:
        return analysis, "metadata", "初步分析 · 元数据", "仅能依据标题、正文、互动等页面信息进行初步判断"
    return analysis, "unverified", "证据待核验", "已填写分析，但尚未提供可核验的视频内容证据"


def _confidence(value: Any) -> str:
    text = _text(value).strip()
    return {"high": "高", "medium": "中", "low": "低", "unknown": "待评估"}.get(text.lower(), text or "待评估")


def _paragraph(value: Any, fallback: str = "待补充") -> str:
    text = _text(value).strip()
    return f'<p class="prose">{_h(text or fallback)}</p>'


def _items(value: Any, fallback: str) -> str:
    items = [_text(item).strip() for item in _list(value) if _text(item).strip()]
    if not items:
        return f'<p class="muted">{_h(fallback)}</p>'
    return '<ul class="plain-list">' + "".join(f"<li>{_h(item)}</li>" for item in items) + "</ul>"


def _timecode(value: Any) -> str:
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        minutes, seconds = divmod(int(value), 60)
        return f"{minutes:02d}:{seconds:02d}"
    return _text(value)


def _snapshot_caption(sample: dict) -> str:
    """Keep the local import time distinct from historical index metrics."""
    collected = _display_time(sample.get("collected_at"))
    indexed = sample.get("source_type") == "public_web_search" or sample.get("metric_observation_type") == "search_index_snapshot"
    if not indexed:
        return f"互动快照采集于：{collected} · 缺失指标以 — 表示"
    snapshot = sample.get("source_snapshot")
    index_date = sample.get("source_index_crawled_at")
    if not index_date and isinstance(snapshot, dict):
        index_date = snapshot.get("index_crawled_at") or snapshot.get("crawled_at") or snapshot.get("indexed_at")
    if not index_date:
        for observation in _list(sample.get("content_evidence") or sample.get("evidence")):
            item = _mapping(observation)
            if item.get("index_crawl_description"):
                index_date = item["index_crawl_description"]
                break
    observed = _text(index_date) or "未提供"
    return f"网页索引观察（非实时指标） · 索引抓取标注：{observed} · 本地收录：{collected} · 缺失指标以 — 表示"


def _card(sample: dict, index: int) -> tuple[str, list[str], str]:
    platform = _text(sample.get("platform")) or "unknown"
    platform_name = _PLATFORMS.get(platform, platform)
    analysis, evidence_group, evidence_label, evidence_note = _analysis_info(sample)
    title = sample.get("title") or sample.get("caption") or "未提供标题"
    topics = _topics(sample)
    tags = [_text(tag).lstrip("#") for tag in _list(sample.get("tags")) if _text(tag).strip()]
    evidence = _resolved_evidence(sample, analysis)
    evidence_ids = {_text(item.get("id")): f"evidence-{index}-{i}" for i, item in enumerate(evidence, 1) if item.get("id")}
    search_values = [sample.get(field) for field in ("title", "description", "caption", "author_name", "author", "tags", "selection_reason", "id")]
    search_values.extend([topics, analysis.get("content_summary"), analysis.get("transferable_idea"), analysis.get("observed_evidence"), analysis.get("hypotheses")])
    search_values.extend(item.get("text") for item in evidence)
    search_text = " ".join(_text(value) for value in search_values).lower()
    likes = _number(_metric(sample, "likes"))
    link = _link(sample.get("url") or sample.get("source_url"), "打开原视频 ↗", "video-link")
    if not link:
        link = '<span class="video-link unavailable">原视频链接缺失或不可用</span>'
    author = sample.get("author_name") or sample.get("author") or "作者未提供"
    if isinstance(author, dict):
        author = author.get("name") or author.get("nickname") or "作者未提供"
    metric_html = "".join(
        f'<div><dt>{label}</dt><dd>{_h(_format_count(_metric(sample, key)))}</dd></div>'
        for key, label in (("likes", "点赞"), ("comments", "评论"), ("favorites", "收藏"), ("shares", "分享"), ("views", "播放"))
    )
    observed_html = []
    for i, item in enumerate(evidence, 1):
        eid = _text(item.get("id")) or f"E{i}"
        kind = _text(item.get("type"))
        source_link = _link(item.get("url"), "证据来源 ↗", "source-link")
        start, end = item.get("start"), item.get("end")
        timestamp = ""
        if start is not None or end is not None:
            timestamp = f'<span class="timecode">{_h(_timecode(start) or "起点未标注")} → {_h(_timecode(end) or "终点未标注")}</span>'
        elif item.get("timestamp_seconds") is not None:
            timestamp = f'<span class="timecode">{_h(_timecode(item["timestamp_seconds"]))}</span>'
        observed_html.append(
            f'<li id="evidence-{index}-{i}"><div class="evidence-heading"><span class="evidence-id">{_h(eid)}</span>'
            f'<span>{_h(_EVIDENCE_TYPES.get(kind, kind or "类型未标注"))}</span>{timestamp}{source_link}</div>'
            f'{_paragraph(item.get("text"), "证据内容未填写")}</li>'
        )
    observed_block = '<ol class="evidence-list">' + "".join(observed_html) + "</ol>" if observed_html else '<p class="muted">尚未记录可核验的观察证据。</p>'
    hypothesis_html = []
    for hypothesis in _list(analysis.get("hypotheses")):
        item = _mapping(hypothesis)
        if not item:
            item = {"text": _text(hypothesis)}
        references = []
        for evidence_id in _list(item.get("evidence_ids")):
            evidence_id = _text(evidence_id)
            if evidence_id in evidence_ids:
                references.append(f'<a href="#{evidence_ids[evidence_id]}" class="evidence-ref">{_h(evidence_id)}</a>')
            else:
                references.append(f'<span class="missing-ref">{_h(evidence_id)}（引用待核验）</span>')
        refs = " ".join(references) or '<span class="missing-ref">尚未关联观察证据</span>'
        hypothesis_html.append(f'<li>{_paragraph(item.get("claim") or item.get("text"), "推断内容未填写")}<div class="hypothesis-meta">{refs}<span>置信度：{_h(_confidence(item.get("confidence") or analysis.get("confidence")))}</span></div></li>')
    hypotheses_block = '<ol class="hypothesis-list">' + "".join(hypothesis_html) + "</ol>" if hypothesis_html else '<p class="muted">待根据具体证据分析传播因素。</p>'
    topic_html = "".join(f'<span class="topic-tag">{_h(topic)}</span>' for topic in topics)
    tags_html = "".join(f'<span>#{_h(tag)}</span>' for tag in tags[:16])
    limitations = _list(analysis.get("limitations"))
    if evidence_group == "metadata":
        limitations = ["当前为初步分析：尚无已核验的字幕、转录或实际视频画面证据。", *limitations]
    if evidence_group == "pending":
        limitations = ["本条尚未完成逐条分析。", *limitations]
    if evidence_group == "invalid":
        limitations = ["分析核验未通过，不能视为已完成的视频拆解。", *limitations]
    if analysis.get("validation_errors"):
        limitations.extend(f"校验记录：{_text(item)}" for item in _list(analysis["validation_errors"]))
    access_status = sample.get("url_status") or sample.get("link_status") or sample.get("media_access_status") or sample.get("access_status")
    access_html = f'<span>链接／素材状态：{_h(access_status)}</span>' if access_status else '<span>链接访问状态：未提供核验记录</span>'
    source_snapshot = '<h4>来源快照</h4>' + _paragraph(sample["source_snapshot"]) if sample.get("source_snapshot") else ""
    snapshot_class = "snapshot indexed" if sample.get("source_type") == "public_web_search" or sample.get("metric_observation_type") == "search_index_snapshot" else "snapshot"
    card = f'''
<article id="video-{index}" class="video-card" data-platform="{_h(platform)}" data-topics="{_h(json.dumps(topics, ensure_ascii=False))}" data-evidence="{evidence_group}" data-search="{_h(search_text)}" data-likes="{likes if likes is not None else -1}" data-index="{index}">
  <div class="card-topline"><span class="card-number">{index:02d}</span><span class="platform-pill {"douyin" if platform == "douyin" else "xhs"}">{_h(platform_name)}</span><span class="evidence-badge {evidence_group}" title="{_h(evidence_note)}">{_h(evidence_label)}</span></div>
  <h3>{_h(title)}</h3>
  <div class="byline"><span>{_h(author)}</span><span>发布：{_h(_display_time(sample.get("published_at")))}</span></div>
  <div class="topic-tags">{topic_html}</div>
  <dl class="metrics">{metric_html}</dl>
  <div class="{snapshot_class}">{_h(_snapshot_caption(sample))}</div>
  <div class="card-actions">{link}<span class="content-id">作品 ID：{_h(sample.get("id") or sample.get("sample_id") or "未提供")}</span></div>
  <div class="access-status">{access_html}</div>
  <div class="summary-box"><h4>内容摘要</h4>{_paragraph(analysis.get("content_summary"), "待分析；尚未形成基于视频内容的摘要。")}</div>
  <div class="selection-note"><strong>入选依据</strong>{_paragraph(sample.get("selection_reason"), "入选依据尚未提供。")}</div>
  <details class="analysis-details" open><summary>为什么可能火 <span>解释性推断 · {_h(_confidence(analysis.get("confidence")))}置信度</span></summary>{hypotheses_block}</details>
  <div class="idea-box"><h4>可以借鉴的选题／表达</h4>{_paragraph(analysis.get("transferable_idea"), "待从内容证据中提炼。")}</div>
  <details class="evidence-details"><summary>观察证据 <span>{len(evidence)} 条</span></summary>{observed_block}</details>
  <details class="limitations-details"><summary>局限与待核验项</summary>{_items(limitations, "未单独填写；单次互动快照无法证明传播因素的因果关系。")}</details>
  <details class="source-details"><summary>原始文案与标签</summary>{_paragraph(sample.get("description") or sample.get("caption"), "未提供原始文案。 ")}<div class="raw-tags">{tags_html or '<span>未提供标签</span>'}</div>{source_snapshot}</details>
</article>'''
    return card, topics, evidence_group


_CSS = """
.insights-panel{margin:22px 0}.insights-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:14px}.insight-card{padding:18px;border:1px solid var(--line);border-radius:10px;background:#f7faf5;overflow-wrap:anywhere}.insight-card h3{font-size:16px;margin:0 0 12px;line-height:1.6}.insight-label{font-size:11px;font-weight:650;color:var(--teal);margin:13px 0 5px}.insight-card .prose{font-size:12px}.insight-refs{display:flex;gap:6px;flex-wrap:wrap;margin-top:14px;font-size:11px;color:var(--muted)}.insight-ref{padding:2px 7px;background:#e8f0e4;border-radius:4px;text-decoration:none}.video-card{scroll-margin-top:20px}.video-card:target{outline:2px solid #6e9475;outline-offset:3px}
.hero h1{overflow-wrap:anywhere}.evidence-badge.invalid{background:#fce9e5;color:#a45543}.snapshot.indexed{color:#956432;background:#fff4e6;border-radius:5px;padding:7px 9px}
:root{color-scheme:light;--ink:#182a30;--muted:#66797c;--line:#dce6e4;--paper:#f3f6f2;--teal:#17695b;--teal-light:#e6f2ec;--orange:#c25b2a;--shadow:0 8px 30px #193c3010}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.65}a{color:var(--teal);text-decoration-thickness:1px;text-underline-offset:3px}a:focus-visible,button:focus-visible,select:focus-visible,input:focus-visible,summary:focus-visible{outline:3px solid #ce7c42;outline-offset:4px}[hidden]{display:none!important}.shell{width:min(1240px,calc(100% - 64px));margin:auto}.hero{background:#153e37;color:#f5faf5;padding:42px 0 38px;position:relative;overflow:hidden}.hero:after{content:"";position:absolute;width:330px;height:330px;border:1px solid #ffffff12;border-radius:50%;right:-80px;top:-110px;pointer-events:none}.hero-label{font-size:12px;letter-spacing:2px;color:#b2d1bd;font-weight:650}.hero-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin:16px 0 10px}.hero h1{font-size:clamp(26px,3.4vw,42px);letter-spacing:-1px;line-height:1.3;margin:0;max-width:900px}.run-status{flex-shrink:0;border:1px solid #bcd0b54a;background:#ffffff0d;border-radius:100px;padding:6px 14px;font-size:13px;color:#dcebd9}.hero-description{margin:15px 0;color:#c3d7ce;font-size:14px;max-width:850px}.hero-footer{display:flex;flex-wrap:wrap;gap:8px 22px;color:#a8c4b6;font-size:12px}.dashboard{display:grid;grid-template-columns:1.35fr 1fr 1fr 1fr;gap:14px;margin-top:24px}.stat{background:#fff;padding:20px 22px;border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}.stat-label{font-size:12px;letter-spacing:.5px;color:var(--muted)}.stat-value{font-size:30px;line-height:1.4;font-weight:700;margin-top:6px;font-variant-numeric:tabular-nums}.stat-value small{font-size:16px;color:#8b9a95;font-weight:400}.stat-note{font-size:12px;color:var(--muted);margin-top:4px}.progress{height:4px;border-radius:2px;background:#e8eee9;margin-top:13px;overflow:hidden}.progress i{height:100%;display:block;background:#5e9471;border-radius:2px}.collection-panel{margin:20px 0 22px;padding:20px 24px;background:#fff;border:1px solid var(--line);border-radius:14px}.section-eyebrow{font-size:11px;font-weight:700;letter-spacing:1.8px;color:var(--teal)}.collection-panel h2{font-size:16px;margin:4px 0 14px}.collection-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.collection-item{border:1px solid var(--line);border-radius:10px;padding:14px 16px}.collection-head{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:14px}.collection-state{font-size:12px;background:#f1f5ef;border-radius:20px;padding:2px 10px;color:#527352}.collection-reason{font-size:13px;color:var(--muted);margin:8px 0 0;white-space:pre-wrap;overflow-wrap:anywhere}.collection-extra{font-size:12px;color:var(--muted);margin-top:8px}.method-note{font-size:12px;color:var(--muted);margin:12px 0 0}.filter-panel{background:#eaf0e9;border:1px solid #d4e1d4;border-radius:14px;padding:20px 22px}.filter-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.filter-heading h2{font-size:18px;margin:0}.reset-button{font:inherit;font-size:12px;border:0;background:none;color:var(--teal);cursor:pointer;padding:5px}.filters{display:grid;grid-template-columns:1.6fr 1fr 1.3fr 1.2fr 1.2fr;gap:12px}.filter-field label{display:block;font-size:11px;color:#5b7267;margin-bottom:5px}.filter-field input,.filter-field select{width:100%;border:1px solid #cfdbd0;background:#fff;color:var(--ink);font:inherit;font-size:13px;height:41px;padding:8px 11px;border-radius:8px}.results-line{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;font-size:12px;color:var(--muted);padding:14px 2px 0}.platform-section{margin:30px 0}.platform-title{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:15px}.platform-title h2{font-size:22px;letter-spacing:-.5px;margin:0}.platform-count{font-size:13px;color:var(--muted)}.video-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;align-items:start}.video-card{min-width:0;background:#fff;border:1px solid var(--line);border-radius:16px;padding:24px;box-shadow:var(--shadow);overflow-wrap:anywhere}.card-topline{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.card-number{font-size:12px;font-weight:650;color:#8b9a92;font-variant-numeric:tabular-nums;margin-right:3px}.platform-pill{font-size:11px;padding:3px 9px;border-radius:5px;font-weight:650}.platform-pill.douyin{background:#edf1f2;color:#34424b}.platform-pill.xhs{background:#fceeed;color:#b95050}.evidence-badge{font-size:10px;padding:3px 8px;border-radius:5px;color:#627067;background:#f0f3ef;margin-left:auto}.evidence-badge.content{background:#e7f3eb;color:#287251}.evidence-badge.metadata,.evidence-badge.unverified{background:#fff2df;color:#986325}.video-card h3{font-size:21px;letter-spacing:-.4px;line-height:1.5;margin:17px 0 9px}.byline{display:flex;flex-wrap:wrap;gap:4px 14px;color:var(--muted);font-size:12px}.topic-tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}.topic-tag{font-size:10px;color:#60796a;background:#f1f6ef;border-radius:4px;padding:2px 7px}.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));padding:15px 0 13px;margin:17px 0 0;border-top:1px solid #e8eeea;border-bottom:1px solid #e8eeea;gap:5px}.metrics dt{font-size:10px;color:var(--muted)}.metrics dd{font-size:17px;font-weight:650;font-variant-numeric:tabular-nums;letter-spacing:-.3px;margin:3px 0 0}.snapshot{font-size:10px;color:#85918b;margin:8px 0 13px}.card-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.video-link{display:inline-flex;align-items:center;justify-content:center;padding:7px 12px;background:var(--teal);border-radius:6px;color:#fff;font-size:12px;text-decoration:none}.video-link.unavailable{background:#eef1ed;color:#7e8d84}.content-id{font-size:10px;color:#7b8c81;overflow-wrap:anywhere}.access-status{color:#819086;font-size:10px;margin:8px 0 18px}.summary-box{border-left:3px solid #81aa88;background:#f4f8f2;padding:13px 15px}.video-card h4{font-size:12px;font-weight:650;margin:0 0 5px;color:#456b54}.prose{white-space:pre-wrap;margin:0;font-size:13px;line-height:1.85}.selection-note{display:flex;align-items:flex-start;gap:10px;margin:15px 0;color:var(--muted)}.selection-note strong{font-size:11px;white-space:nowrap;margin-top:3px;color:#55765e}.selection-note .prose{font-size:12px}details{margin-top:15px;border-top:1px solid #e4ebe5;padding-top:13px}summary{font-size:12px;font-weight:650;cursor:pointer;list-style-position:outside;margin-left:13px}summary span{font-size:10px;font-weight:400;color:var(--muted);margin-left:7px}details .muted{font-size:12px;color:var(--muted);margin:12px 0 4px}.hypothesis-list{padding-left:20px;margin:13px 0 0}.hypothesis-list li{padding-left:2px;margin:12px 0}.hypothesis-list li::marker{color:#81a084;font-size:12px}.hypothesis-meta{display:flex;flex-wrap:wrap;align-items:center;gap:5px 9px;font-size:10px;color:var(--muted);margin-top:6px}.evidence-ref{font-size:10px;background:#edf3eb;border-radius:4px;text-decoration:none;padding:1px 6px}.missing-ref{font-size:10px;color:#a4764a}.idea-box{background:#fff6e9;padding:13px 15px;border-radius:8px;margin-top:16px}.idea-box h4{color:#9c6b37}.idea-box .prose{font-size:12px;color:#765d3f}.evidence-list{list-style:none;padding:0;margin:12px 0 0}.evidence-list>li{background:#f6f8f5;padding:12px;border-radius:7px;margin-bottom:8px;scroll-margin-top:20px}.evidence-heading{display:flex;flex-wrap:wrap;gap:5px 9px;align-items:center;font-size:10px;color:#6d7c70;margin-bottom:7px}.evidence-id{color:#386e49;font-weight:700}.source-link{margin-left:auto}.timecode{font-variant-numeric:tabular-nums;color:#8a8f7a}.evidence-list .prose{font-size:12px}.plain-list{font-size:12px;color:var(--muted);padding-left:18px;margin:11px 0 0}.plain-list li{margin:6px 0;white-space:pre-wrap}.source-details .prose{font-size:12px;color:var(--muted);margin-top:12px}.raw-tags{display:flex;flex-wrap:wrap;gap:6px;font-size:10px;color:#6d8773;margin-top:9px}.empty-state{background:#fff;border:1px dashed #c9d7c8;border-radius:16px;text-align:center;padding:54px 24px;margin:26px 0}.empty-state .empty-icon{display:inline-flex;width:50px;height:50px;align-items:center;justify-content:center;background:#edf3e9;border-radius:50%;font-size:25px;color:#729567}.empty-state h2{font-size:20px;margin:16px 0 8px}.empty-state p{font-size:13px;color:var(--muted);max-width:650px;margin:0 auto;white-space:pre-wrap}.details-panel{background:#fff;border:1px solid var(--line);border-radius:12px;padding:0 20px 15px;margin:24px 0}.details-panel summary{padding-top:14px}.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px 20px;margin-top:13px}.stats-grid div{display:flex;gap:8px;font-size:12px;align-items:flex-start}.stats-grid dt{color:var(--muted);min-width:90px}.stats-grid dd{margin:0;overflow-wrap:anywhere}.report-footer{padding:24px 0 38px;border-top:1px solid #d5e1d3;color:#7c8d7b;font-size:11px}.report-footer p{margin:4px 0}.noscript{display:block;margin:15px 0;font-size:12px;color:#8d6b3b}.print-help{color:#9ab2a0}@media(min-width:1600px){.shell{width:1320px}}@media(max-width:1000px){.filters{grid-template-columns:repeat(3,minmax(0,1fr))}.filter-field:first-child{grid-column:span 2}.dashboard{grid-template-columns:repeat(2,minmax(0,1fr))}.video-card{padding:20px}.metrics dd{font-size:15px}}@media(max-width:680px){.shell{width:calc(100% - 28px)}.hero{padding:28px 0}.hero-heading{display:block}.hero h1{font-size:29px}.run-status{display:inline-block;margin-top:14px}.hero-description{font-size:12px}.dashboard{gap:9px;margin-top:14px}.stat{padding:15px}.stat-value{font-size:25px}.stat-note{font-size:10px}.collection-panel{padding:17px}.collection-grid,.video-grid{grid-template-columns:1fr}.filter-panel{padding:16px}.filters{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.filter-field:first-child{grid-column:span 2}.filter-field:last-child{grid-column:span 2}.platform-section{margin:23px 0}.video-card{padding:19px}.metrics dd{font-size:17px}.platform-title h2{font-size:20px}.evidence-badge{margin-left:0}.results-line{font-size:10px}.report-footer{font-size:10px}.collection-head{font-size:13px}.hero-label{font-size:10px}.collection-state{max-width:70%;text-align:right}.stats-grid{grid-template-columns:1fr}}@media print{body{background:white}.shell{width:100%}.hero{padding:20px 0;background:#fff;color:#111}.hero-label,.hero-description,.hero-footer{color:#555}.run-status{color:#444;border-color:#ccc}.filter-panel,.reset-button,.results-line,.print-help{display:none}.dashboard{margin-top:12px}.stat,.video-card,.collection-panel{box-shadow:none}.video-grid{display:block}.video-card{break-inside:avoid;margin-bottom:18px}a{color:#17695b}.hero:after{display:none}.video-link{background:#fff;border:1px solid #aaa;color:#111}.platform-section{margin-top:20px}.collection-panel{break-inside:avoid}}
"""


_JS = """
(() => {
  'use strict';
  const filters = {
    search: document.getElementById('search'),
    platform: document.getElementById('platform-filter'),
    topic: document.getElementById('topic-filter'),
    evidence: document.getElementById('evidence-filter'),
    sort: document.getElementById('sort-order')
  };
  const cards = Array.from(document.querySelectorAll('.video-card'));
  const sections = Array.from(document.querySelectorAll('.platform-section'));
  const empty = document.getElementById('no-matches');
  const results = document.getElementById('result-count');
  function update() {
    const query = filters.search.value.trim().toLocaleLowerCase();
    let count = 0;
    cards.forEach(card => {
      const topics = JSON.parse(card.dataset.topics || '[]');
      const matches = (!query || card.dataset.search.includes(query)) &&
        (!filters.platform.value || card.dataset.platform === filters.platform.value) &&
        (!filters.topic.value || topics.includes(filters.topic.value)) &&
        (!filters.evidence.value || card.dataset.evidence === filters.evidence.value);
      card.hidden = !matches;
      if (matches) count += 1;
    });
    sections.forEach(section => {
      const grid = section.querySelector('.video-grid');
      const group = Array.from(grid.querySelectorAll('.video-card'));
      const mode = filters.sort.value;
      group.sort((a, b) => {
        const aLikes = Number(a.dataset.likes), bLikes = Number(b.dataset.likes);
        if (mode === 'likes-desc' || mode === 'likes-asc') {
          // Missing metrics always go last, including ascending mode.
          if (aLikes < 0 && bLikes >= 0) return 1;
          if (bLikes < 0 && aLikes >= 0) return -1;
          const delta = mode === 'likes-desc' ? bLikes - aLikes : aLikes - bLikes;
          if (delta) return delta;
        }
        return Number(a.dataset.index) - Number(b.dataset.index);
      });
      group.forEach(card => grid.appendChild(card));
      const visible = group.filter(card => !card.hidden).length;
      section.hidden = visible === 0;
      section.querySelector('.visible-platform-count').textContent = String(visible);
    });
    results.textContent = '当前显示 ' + count + ' / ' + cards.length + ' 条';
    empty.hidden = count !== 0 || cards.length === 0;
  }
  Object.values(filters).forEach(control => {
    control.addEventListener(control === filters.search ? 'input' : 'change', update);
  });
  function resetFilters() {
    filters.search.value = '';
    filters.platform.value = '';
    filters.topic.value = '';
    filters.evidence.value = '';
    filters.sort.value = 'original';
    update();
  }
  document.getElementById('reset-filters').addEventListener('click', resetFilters);
  document.querySelectorAll('.insight-ref').forEach(link => {
    link.addEventListener('click', resetFilters);
  });
  update();
})();
"""


def _collection_status(result: dict, counts: Counter, quotas: dict) -> str:
    statuses = _mapping(result.get("collection_status"))
    platforms = list(dict.fromkeys([*_PLATFORMS, *statuses, *counts]))
    blocks = []
    for platform in platforms:
        info = statuses.get(platform)
        status = _mapping(info)
        state = _text(status.get("status") or (info if isinstance(info, str) else "pending"))
        reason = status.get("reason") or status.get("error") or status.get("message")
        if not reason:
            reason = "本次未提供详细采集状态。" if counts[platform] else "尚无可展示的真实入选视频；查看采集记录后补充。"
        quota = quotas.get(platform)
        quota_text = f" / 配额 {_text(quota)}" if quota is not None else ""
        extra = []
        for key, label in (("candidate_count", "候选"), ("collected_count", "已采集"), ("selected_count", "入选"), ("source", "入口"), ("collected_at", "采集时间")):
            if status.get(key) is not None:
                value = _display_time(status[key]) if key == "collected_at" else _text(status[key])
                extra.append(f"{label}：{value}")
        blocks.append(f'''<div class="collection-item"><div class="collection-head"><strong>{_h(_PLATFORMS.get(platform, platform))} · {counts[platform]} 条{_h(quota_text)}</strong><span class="collection-state">{_h(_STATUSES.get(state, state or "待核验"))}</span></div><p class="collection-reason">{_h(reason)}</p>{'<div class="collection-extra">' + _h(' · '.join(extra)) + '</div>' if extra else ''}</div>''')
    return "".join(blocks)


def _selection_stats(stats: dict) -> str:
    if not stats:
        return ""
    labels = {
        "candidate_total": "候选总数", "candidates_total": "候选总数",
        "selected_total": "入选总数", "deduplicated_total": "去重后数量",
        "rejected_total": "未入选数量", "video_count": "视频数量",
        "non_video_rejected": "非视频排除", "duplicate_count": "重复数量",
        "topic_rejected": "主题不符排除", "low_heat_rejected": "热度不足排除",
        "min_likes": "最低点赞门槛", "time_window_days": "时间窗口（天）",
        "per_platform": "各平台记录", "rejection_reasons": "未入选原因",
        "rules": "筛选规则", "thresholds": "热度门槛", "warnings": "待核验项",
        "input_count": "输入条目数", "normalized_count": "标准化后数量",
        "unique_count": "去重后数量", "selected_count": "入选数量",
        "target_count": "目标数量", "quotas": "平台配额",
        "platform_counts": "各平台入选数", "eligible_counts": "各平台合格数",
        "missing_by_platform": "各平台缺额", "unidentifiable_count": "无法识别数量",
        "excluded_counts": "排除原因与数量", "recent_days": "时间窗口（天）",
        "as_of": "筛选基准时间",
        "raw_platform_candidates": "实采原始候选（去重）", "curation_method": "人工选样方式",
    }
    fields = "".join(f'<div><dt>{_h(labels.get(_text(key), key))}</dt><dd>{_h(value)}</dd></div>' for key, value in stats.items())
    return f'<details class="details-panel"><summary>选样记录与规则</summary><dl class="stats-grid">{fields}</dl></details>'


def _insights(insights: Any, samples: list[dict]) -> str:
    """Display supplied observations only, with unambiguous sample anchors."""
    anchors = {}
    bare_ids = Counter(_text(sample.get("id")) for sample in samples)
    for index, sample in enumerate(samples, 1):
        sample_id = _text(sample.get("sample_id"))
        item_id = _text(sample.get("id"))
        platform = _text(sample.get("platform"))
        target = (f"video-{index}", f"{_PLATFORMS.get(platform, platform)} · 样本 {index:02d}")
        if sample_id:
            anchors[sample_id] = target
        if platform and item_id:
            anchors[f"{platform}:{item_id}"] = target
        if item_id and bare_ids[item_id] == 1:
            anchors[item_id] = target
    blocks = []
    for value in _list(insights):
        item = _mapping(value)
        if not item or not any(item.get(key) for key in ("title", "observation", "application", "sample_ids")):
            continue
        references = []
        for sample_id in dict.fromkeys(_text(value) for value in _list(item.get("sample_ids"))):
            if sample_id in anchors:
                target, label = anchors[sample_id]
                references.append(f'<a class="insight-ref" href="#{target}" title="{_h(sample_id)}">{_h(label)}</a>')
            elif sample_id:
                references.append(f'<span>{_h(sample_id)}（本页无对应样本）</span>')
        reference_html = '<div class="insight-refs"><span>参考样本：</span>' + "".join(references) + '</div>' if references else ""
        observation = '<div class="insight-label">样本观察</div>' + _paragraph(item["observation"]) if item.get("observation") else ""
        application = '<div class="insight-label">可以借鉴的做法</div>' + _paragraph(item["application"]) if item.get("application") else ""
        title = f'<h3>{_h(item["title"])}</h3>' if item.get("title") else ""
        blocks.append(f'<article class="insight-card">{title}{observation}{application}{reference_html}</article>')
    if not blocks:
        return ""
    return '<section class="collection-panel insights-panel" aria-labelledby="insights-heading"><div class="section-eyebrow">PATTERNS IN THIS BATCH</div><h2 id="insights-heading">本批样本的共同模式</h2><div class="insights-grid">' + "".join(blocks) + '</div></section>'


def render_report(result: dict, output: Path) -> Path:
    """Write an offline HTML report to ``output`` and return its path.

    ``result['samples']`` accepts mappings or dataclass instances. Totals are
    derived from those actual samples. This renderer never invents videos,
    metrics, evidence, or source links to fill a target quota.
    """
    result = _mapping(result)
    samples = [_mapping(sample) for sample in _list(result.get("samples"))]
    samples = [sample for sample in samples if sample]
    quotas = _mapping(result.get("quotas")) or {"douyin": 25, "xiaohongshu": 25}
    target_value = _number(result.get("target_total", 50))
    target = int(target_value) if target_value is not None else 50
    counts = Counter(_text(sample.get("platform")) or "unknown" for sample in samples)
    content_count = 0
    preliminary_count = 0
    pending_count = 0
    topics = set()
    groups: dict[str, list[str]] = {platform: [] for platform in _PLATFORMS}
    for index, sample in enumerate(samples, 1):
        card, card_topics, evidence_group = _card(sample, index)
        topics.update(card_topics)
        groups.setdefault(_text(sample.get("platform")) or "unknown", []).append(card)
        content_count += evidence_group == "content"
        preliminary_count += evidence_group in {"metadata", "unverified", "invalid"}
        pending_count += evidence_group == "pending"
    total = len(samples)
    supplied_status = _text(result.get("status")) or "partial"
    # A complete status needs actual samples and actual video-content evidence.
    quotas_met = all(counts[platform] >= (_number(quota) or 0) for platform, quota in quotas.items())
    declared_complete = supplied_status in {"complete", "completed", "success"}
    achieved = total >= target and content_count == total and quotas_met
    status_label = "已完成" if declared_complete and achieved else _STATUSES.get(supplied_status, supplied_status)
    if declared_complete and not achieved:
        status_label = "部分完成 · 仍有缺项"
    title = _text(result.get("title")) or "公开视频参考研究"
    generated_at = result.get("generated_at") or "未提供生成时间"
    generation_note = f"生成于 {_display_time(generated_at)} · 时间按洛杉矶显示"
    platform_options = "".join(f'<option value="{_h(platform)}">{_h(_PLATFORMS.get(platform, platform))}</option>' for platform in groups)
    topic_options = "".join(f'<option value="{_h(topic)}">{_h(topic)}</option>' for topic in sorted(topics))
    sections = "".join(
        f'<section class="platform-section" data-platform="{_h(platform)}"><div class="platform-title"><h2>{_h(_PLATFORMS.get(platform, platform))} <span class="platform-count">视频样本</span></h2><span class="platform-count"><span class="visible-platform-count">{len(cards)}</span> 条显示 · 共 {len(cards)} 条</span></div><div class="video-grid">{"".join(cards)}</div></section>'
        for platform, cards in groups.items() if cards
    )
    zero_state = ""
    if not samples:
        zero_state = '<div class="empty-state"><div class="empty-icon" aria-hidden="true">○</div><h2>还没有可展示的真实视频样本</h2><p>本页保留本次采集状态与目标配额。获得符合主题、可定位的真实视频后，才能补充互动快照与逐条分析。</p></div>'
    remaining = max(0, target - total)
    ratio = min(100, total / target * 100) if target > 0 else 0
    quota_summary = " · ".join(f"{_PLATFORMS.get(platform, platform)} {counts[platform]}/{_text(quota)}" for platform, quota in quotas.items())
    content_note = f"初步／待核验 {preliminary_count} 条 · 待分析 {pending_count} 条"
    actual_note = f"距目标还差 {remaining} 条" if remaining else "已达到数量目标，仍需核对配额与证据"
    summary = "公开互动用于筛选候选；逐条分析分开呈现观察证据、传播因素推断和可以借鉴的表达。"
    method_note = f'<p class="method-note">{_h(result["method_note"])}</p>' if result.get("method_note") else ""
    document = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{_h(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<header class="hero"><div class="shell"><div class="hero-label">DYXHS GUIDER / 内容研究档案</div><div class="hero-heading"><h1>{_h(title)}</h1><span class="run-status">{_h(status_label)}</span></div><p class="hero-description">{_h(summary)}</p><div class="hero-footer"><span>{_h(generation_note)}</span><span>离线可读 · 原视频链接需联网访问</span><span>排序按平台分别进行</span></div></div></header>
<main class="shell">
<section class="dashboard" aria-label="本次研究概况">
  <div class="stat"><div class="stat-label">真实入选视频 / 目标</div><div class="stat-value">{total} <small>/ {target}</small></div><div class="stat-note">{_h(actual_note)}</div><div class="progress" role="progressbar" aria-label="样本数量进度" aria-valuenow="{min(total, max(target, 0))}" aria-valuemin="0" aria-valuemax="{max(target, 1)}"><i style="width:{ratio:.1f}%"></i></div></div>
  <div class="stat"><div class="stat-label">有视频内容证据的分析</div><div class="stat-value">{content_count} <small>条</small></div><div class="stat-note">{_h(content_note)}</div></div>
  <div class="stat"><div class="stat-label">抖音入选</div><div class="stat-value">{counts['douyin']} <small>/ {_h(quotas.get('douyin', '未设配额'))}</small></div><div class="stat-note">按本平台的可见互动筛选</div></div>
  <div class="stat"><div class="stat-label">小红书入选</div><div class="stat-value">{counts['xiaohongshu']} <small>/ {_h(quotas.get('xiaohongshu', '未设配额'))}</small></div><div class="stat-note">保留点赞、收藏等原始指标</div></div>
</section>
<section class="collection-panel" aria-labelledby="collection-heading"><div class="section-eyebrow">COLLECTION STATUS</div><h2 id="collection-heading">采集进展与未完成原因</h2><div class="collection-grid">{_collection_status(result, counts, quotas)}</div><p class="method-note">{_h(quota_summary)}。公开搜索结果只代表本次可访问的候选范围；不等同于全平台热门榜。</p>{method_note}</section>
{_insights(result.get('insights'), samples)}
<section class="filter-panel" aria-labelledby="filter-heading"><div class="filter-heading"><h2 id="filter-heading">浏览研究样本</h2><button id="reset-filters" class="reset-button" type="button">重置筛选 ↺</button></div><div class="filters">
  <div class="filter-field"><label for="search">搜索标题、作者、内容或证据</label><input id="search" type="search" placeholder="输入本批次相关关键词" autocomplete="off"></div>
  <div class="filter-field"><label for="platform-filter">平台</label><select id="platform-filter"><option value="">全部平台</option>{platform_options}</select></div>
  <div class="filter-field"><label for="topic-filter">主题</label><select id="topic-filter"><option value="">全部主题</option>{topic_options}</select></div>
  <div class="filter-field"><label for="evidence-filter">分析与证据</label><select id="evidence-filter"><option value="">全部状态</option><option value="content">有视频内容证据</option><option value="metadata">初步分析 · 元数据</option><option value="unverified">证据待核验</option><option value="invalid">分析核验未通过</option><option value="pending">待分析</option></select></div>
  <div class="filter-field"><label for="sort-order">各平台内部排序</label><select id="sort-order"><option value="original">原始入选顺序</option><option value="likes-desc">点赞从高到低</option><option value="likes-asc">点赞从低到高</option></select></div>
</div><div class="results-line"><span id="result-count" aria-live="polite">当前显示 {total} / {total} 条</span><span>缺失指标排在最后 · 不合并跨平台原始热度</span></div></section>
<noscript><span class="noscript">JavaScript 已关闭，当前展示全部样本；启用后可使用筛选和排序。</span></noscript>
{zero_state}{sections}
<div id="no-matches" class="empty-state" hidden><div class="empty-icon" aria-hidden="true">⌕</div><h2>没有匹配的样本</h2><p>调整平台、主题或证据筛选，也可以清空搜索词。</p></div>
{_selection_stats(_mapping(result.get('selection_stats')))}
<footer class="report-footer"><p>本报告仅基于已记录的公开内容与互动快照，传播因素属于解释性推断，不能据此证明推荐机制或因果关系。未观测到的作者体量、推广和发布时间等因素仍可能影响传播。</p><p>元数据分析标为初步；没有内容证据的样本不计入视频内容证据分析数。所有链接均指向提供的来源地址，小红书临时访问令牌不会写入报告；部分作品可能需要登录或在 App 内定位。</p><p class="print-help">提示：浏览器打印可保存为 PDF；展开证据和局限后打印可保留更多信息。</p></footer>
</main>
<script>{_JS}</script>
</body>
</html>'''
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")
    return output


__all__ = ["render_report"]
