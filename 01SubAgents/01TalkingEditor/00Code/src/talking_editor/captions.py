"""Source-linked captions, edited timeline mapping and portable SRT/ASS export."""
from __future__ import annotations

import logging
import platform
import re
import unicodedata
from pathlib import Path


def _units(text: str) -> float:
    return sum(1.0 if unicodedata.east_asian_width(c) in "WF" else 0.55 for c in text)


def _join(words: list[dict]) -> str:
    parts: list[str] = []
    for word in words:
        token = word["text"].strip()
        if parts and token and re.search(r"[A-Za-z0-9][,;:.!?]?['\"]?$", parts[-1]) and re.match(r"[A-Za-z0-9]", token):
            parts.append(" ")
        parts.append(token)
    text = "".join(parts).strip()
    return re.sub(r"\s+", " ", text)


def _terms(text: str, supplied: list[str]) -> list[str]:
    terms = [s.strip() for s in supplied if s.strip()]
    terms += re.findall(r"\d+(?:[.,]\d+)*(?:%|％|元|万|亿|分钟|小时|天|倍|年)?", text)
    try:
        import jieba
        import jieba.analyse
        jieba.setLogLevel(logging.ERROR)
        terms += jieba.analyse.extract_tags(text, topK=24, allowPOS=("n", "nr", "ns", "nt", "nz", "vn", "eng"))
    except ImportError:
        # No invented semantic emphasis when the optional extractor is absent.
        pass
    stop = {"这个", "那个", "然后", "就是", "觉得", "其实", "可能", "因为", "所以", "我们", "大家", "今天", "视频", "时候", "真的", "还是", "自己", "东西", "什么", "这样", "那么"}
    return list(dict.fromkeys(t for t in terms if t not in stop and (len(t) >= 2 or t.isdigit())))


def _emphasis(text: str, terms: list[str]) -> list[dict]:
    spans: list[dict] = []
    limit = 1 if _units(text) < 10 else 2
    # Global topic relevance determines priority; prefer full terms over overlaps.
    for term in terms:
        for match in re.finditer(re.escape(term), text):
            start, end = match.span()
            if any(start < s["end"] and end > s["start"] for s in spans):
                continue
            if sum(s["end"] - s["start"] for s in spans) + end - start > max(3, len(text) * 0.45):
                continue
            spans.append({"start": start, "end": end, "text": term})
            if len(spans) >= limit:
                return sorted(spans, key=lambda x: x["start"])
    return sorted(spans, key=lambda x: x["start"])


def build_captions(transcript: dict, plan: dict, config: dict | None = None) -> list[dict]:
    """Map source word IDs through half-open kept intervals, then form readable cues."""
    config = config or {}
    line_chars = float(config.get("max_line_chars", 14))
    max_lines = int(config.get("max_lines", 2))
    max_duration = float(config.get("max_duration", 4.2))
    if line_chars < 4 or max_lines not in (1, 2) or max_duration <= 0:
        raise ValueError("Invalid caption layout settings")
    hidden = {i for f in plan.get("fillers", []) if f.get("subtitle_hidden") for i in f["word_ids"]}
    mapped: list[dict] = []
    for word in transcript.get("words", []):
        if word["id"] in hidden or not word["text"].strip():
            continue
        candidates = []
        for index, keep in enumerate(plan["keep"]):
            start = max(word["start"], keep["source_start"])
            end = min(word["end"], keep["source_end"])
            if end > start:
                candidates.append((end - start, index, keep, start, end))
            elif word["start"] == word["end"] and keep["source_start"] <= word["start"] <= keep["source_end"]:
                # Preserve zero-duration ASR words as display anchors. Do not
                # manufacture acoustic alignment or use them as cutting points.
                candidates.append((0.0, index, keep, word["start"], word["end"]))
        if not candidates:
            continue
        _, index, keep, start, end = max(candidates, key=lambda item: item[0])
        mapped.append({**word, "start": keep["output_start"] + start - keep["source_start"],
                       "end": keep["output_start"] + end - keep["source_start"], "keep_index": index})
    groups: list[list[dict]] = []
    current: list[dict] = []
    for word in mapped:
        if current:
            text = _join(current)
            needs_break = (
                word["start"] - current[-1]["end"] > 0.65
                or word["keep_index"] != current[-1]["keep_index"]
                or word["end"] - current[0]["start"] > max_duration
                or _units(_join(current + [word])) > line_chars * max_lines
                or (re.search(r"[。！？!?；;]$", text) and current[-1]["end"] - current[0]["start"] >= 0.8)
            )
            if needs_break:
                groups.append(current)
                current = []
        current.append(word)
    if current:
        groups.append(current)
    stable_groups: list[list[dict]] = []
    for index, group in enumerate(groups):
        if (index + 1 < len(groups) and all(w["start"] == w["end"] for w in group)
                and abs(groups[index + 1][0]["start"] - group[0]["start"]) < 1e-6):
            groups[index + 1][:0] = group
        else:
            stable_groups.append(group)
    groups = stable_groups
    full_text = "\n".join(_join(g) for g in groups)
    terms = _terms(full_text, config.get("keywords", [])) if config.get("highlights", True) else []
    cues = []
    for i, group in enumerate(groups):
        start = max(0.0, group[0]["start"])
        next_start = groups[i + 1][0]["start"] if i + 1 < len(groups) else plan["output_duration"]
        end = min(plan["output_duration"], next_start, max(group[-1]["end"] + 0.10, start + 0.65))
        if end <= start:
            if cues and all(w["start"] == w["end"] for w in group):
                cues[-1]["text"] = _join([{"text": cues[-1]["text"]}, *group])
                cues[-1]["word_ids"].extend(w["id"] for w in group)
                cues[-1]["highlights"] = _emphasis(cues[-1]["text"], terms)
                continue
            if all(w["start"] == w["end"] for w in group):
                # Display-only fallback for an otherwise lost EOF word. The
                # transcript and acoustic alignment remain untouched/uncertain.
                end = plan["output_duration"]
                start = max(0.0, end - 0.65)
            if end <= start:
                continue
        text = _join(group)
        cues.append({"id": len(cues) + 1, "start": round(start, 6), "end": round(end, 6),
                     "text": text, "word_ids": [w["id"] for w in group], "highlights": _emphasis(text, terms)})
    return cues


def _timestamp(seconds: float, centiseconds: bool = False) -> str:
    scale = 100 if centiseconds else 1000
    total = max(0, round(seconds * scale))
    hours, remainder = divmod(total, 3600 * scale)
    minutes, remainder = divmod(remainder, 60 * scale)
    whole, fraction = divmod(remainder, scale)
    if centiseconds:
        return f"{hours}:{minutes:02}:{whole:02}.{fraction:02}"
    return f"{hours:02}:{minutes:02}:{whole:02},{fraction:03}"


def write_srt(cues: list[dict], path: Path) -> None:
    path.write_text("\n\n".join(f"{i}\n{_timestamp(c['start'])} --> {_timestamp(c['end'])}\n{c['text']}" for i, c in enumerate(cues, 1)) + "\n", encoding="utf-8")


def _ass_escape(text: str) -> str:
    # Full-width braces/backslash prevent captions from injecting ASS override tags.
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\r", " ").replace("\n", " ")


def _ass_text(cue: dict, line_chars: float) -> str:
    starts = {span["start"] for span in cue["highlights"]}
    ends = {span["end"] for span in cue["highlights"]}
    output: list[str] = []
    width = 0.0
    for index, char in enumerate(cue["text"]):
        unit = _units(char)
        if width + unit > line_chars:
            output.append(r"\N")
            width = 0.0
        if index in ends:
            output.append(r"{\c&HFFFFFF&}")
        if index in starts:
            output.append(r"{\c&H00D7FF&}")
        output.append(_ass_escape(char))
        width += unit
    return "".join(output)


def write_ass(cues: list[dict], path: Path, width: int, height: int, config: dict | None = None) -> None:
    config = config or {}
    default_font = "Hiragino Sans GB" if platform.system() == "Darwin" else "Noto Sans CJK SC"
    font = str(config.get("font", default_font)).replace(",", " ").replace("\n", " ")
    size = int(config.get("font_size", round(min(width, height) * 0.055)))
    margin_v = round(height * 0.12)
    margin_h = round(width * 0.065)
    outline = max(1.5, size * 0.055)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H0000D7FF,&H00151515,&H80000000,-1,0,0,0,100,100,0,0,1,{outline:.2f},0,2,{margin_h},{margin_h},{margin_v},1
Style: Title,{font},{round(size * 1.15)},&H00FFFFFF,&H0000D7FF,&H00151515,&H80000000,-1,0,0,0,100,100,0,0,1,{outline:.2f},0,8,{margin_h},{margin_h},{round(height * 0.07)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = [f"Dialogue: 0,{_timestamp(c['start'], True)},{_timestamp(c['end'], True)},Default,,0,0,0,,{_ass_text(c, float(config.get('max_line_chars', 14)))}" for c in cues]
    title = str(config.get("title", "")).strip()
    if title and cues:
        title_cue = {"text": title, "highlights": []}
        title_end = float(config.get("title_duration", cues[-1]["end"]))
        events.insert(0, f"Dialogue: 1,0:00:00.00,{_timestamp(title_end, True)},Title,,0,0,0,,{_ass_text(title_cue, float(config.get('max_line_chars', 14)))}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
