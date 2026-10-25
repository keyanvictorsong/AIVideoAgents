"""Sparse, manually positioned basketball captions; no video edits or tracking.

Coordinates are normalized display-frame coordinates for the entire line's
center. Conservative text bounds are checked, but an editor must still inspect
real frames for overlap with people, the ball, basket and play trajectory.
"""

from __future__ import annotations

import math
from pathlib import Path
import platform
import re
from typing import Any

from .captions import _ass_escape, _timestamp, write_srt


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number.")
    try:
        converted = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number.") from exc
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be a finite number.")
    return converted


def _colour(value: Any) -> str:
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", value) if isinstance(value, str) else None
    if not match:
        raise ValueError("Caption colors must use #RRGGBB.")
    rgb = match.group(1).upper()
    return rgb[4:6] + rgb[2:4] + rgb[0:2]


def _style(plan: dict, width: int, height: int) -> dict:
    raw = plan.get("style", {})
    if not isinstance(raw, dict):
        raise ValueError("style must be an object.")
    default_font = "Hiragino Sans GB" if platform.system() == "Darwin" else "Noto Sans CJK SC"
    font = raw.get("font", default_font)
    if (not isinstance(font, str) or not font.strip() or len(font) > 96
            or any(ord(c) < 32 or c in ",{}\\" for c in font)):
        raise ValueError("font must be a plain font-family name without ASS delimiters.")
    size = _number(raw.get("font_size", 76 * height / 1080), "font_size")
    outline = _number(raw.get("outline", 4 * height / 1080), "outline")
    shadow = _number(raw.get("shadow", 2 * height / 1080), "shadow")
    fade = _number(raw.get("fade_ms", 80), "fade_ms")
    inset = _number(raw.get("safe_inset", max(8, height * .012)), "safe_inset")
    if (not 8 <= size <= height / 3 or not 0 <= outline <= 20
            or not 0 <= shadow <= 20 or not 0 <= fade <= 80
            or not 8 <= inset <= min(width, height) / 4):
        raise ValueError("Invalid font size, outline, shadow, safe inset or fade (maximum 80 ms).")
    return {"font": font.strip(), "font_size": size, "outline": outline,
            "shadow": shadow, "fade_ms": fade, "safe_inset": inset,
            "primary": _colour(raw.get("primary_color", "#FFFFFF")),
            "accent": _colour(raw.get("accent_color", "#FFD83D"))}


def _position(value: Any, name: str, width: int, height: int,
              half_width: float, half_height: float) -> tuple[float, float]:
    if not isinstance(value, dict) or set(value) != {"x", "y"}:
        raise ValueError(f"{name} must contain exactly normalized x and y coordinates.")
    x, y = _number(value["x"], f"{name}.x"), _number(value["y"], f"{name}.y")
    if not 0 <= x <= 1 or not 0 <= y <= 1:
        raise ValueError(f"{name} coordinates must be between 0 and 1.")
    px, py = round(x * width, 3), round(y * height, 3)
    if not half_width <= px <= width - half_width or not half_height <= py <= height - half_height:
        raise ValueError(f"{name}: the whole caption and its safety margin must fit inside the frame.")
    return px, py


def _pixel(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def write_captions(plan: dict, metadata: dict, directory: Path) -> list[dict]:
    """Validate reviewed cues and write captions.ass / captions.srt.

    ``placement_mode`` defaults to ``near_action``. Every cue requires a manual
    ``position={x,y}``; optional ``end_position`` requests linear movement for
    that cue's entire duration. Missing positions never fall back to the bottom.
    Returned cues include the pixel anchors and conservative safety estimates.
    SRT remains ordinary text/timing; placement exists only in ASS/video.
    """
    if not isinstance(plan, dict) or plan.get("placement_mode", "near_action") != "near_action":
        raise ValueError("placement_mode must be near_action.")
    cues = plan.get("cues")
    if not isinstance(cues, list) or not cues:
        raise ValueError("caption-plan.json must provide a nonempty cues list.")
    width, height = metadata["width"], metadata["height"]
    if (isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int)
            or not isinstance(height, int) or width < 32 or height < 32):
        raise ValueError("Display width and height must be positive integer dimensions.")
    duration = _number(metadata["video_duration"], "video_duration")
    if duration <= 0:
        raise ValueError("video_duration must be positive.")
    style = _style(plan, width, height)
    previous_end, previous_export_end = 0.0, 0
    reviewed, events = [], []
    for index, cue in enumerate(cues, 1):
        if not isinstance(cue, dict):
            raise ValueError(f"Caption {index} must be an object.")
        start = _number(cue.get("start"), f"Caption {index} start")
        end = _number(cue.get("end"), f"Caption {index} end")
        if start < 0 or end <= start or end > duration + 1e-9:
            raise ValueError(f"Caption {index} is outside the unchanged source timeline.")
        export_start, export_end = round(start * 100), round(end * 100)
        if (start < previous_end - 1e-9 or export_start < previous_export_end
                or export_end <= export_start):
            raise ValueError(f"Caption {index} overlaps, is out of order or collapses at ASS time precision.")
        raw_text = cue.get("text")
        if not isinstance(raw_text, str):
            raise ValueError(f"Caption {index} text must be a short string.")
        text = raw_text.strip()
        if not 2 <= len(text) <= 8 or any(ord(c) < 32 or c in "\u2028\u2029" for c in text):
            raise ValueError(f"Caption {index} must be one short 2–8 character line.")
        if not isinstance(cue.get("evidence"), dict) or not cue["evidence"]:
            raise ValueError(f"Caption {index} must retain its reviewed source evidence.")
        if "accent" in cue and not isinstance(cue["accent"], bool):
            raise ValueError(f"Caption {index} accent must be true or false.")
        cue_colour = cue.get("color", "primary")
        if not isinstance(cue_colour, str) or cue_colour not in {"primary", "accent"}:
            raise ValueError(f"Caption {index} color must be primary or accent.")
        # Deliberately overestimate all glyphs, including Latin and escaped
        # full-width ASS delimiters. This is a safety check, not font/CV proof.
        padding = style["outline"] + style["shadow"] + style["safe_inset"]
        estimated_width = len(text) * style["font_size"] * 1.15 + 2 * padding
        estimated_height = style["font_size"] * 1.5 + 2 * padding
        anchor = _position(cue.get("position"), f"Caption {index} position", width, height,
                           estimated_width / 2, estimated_height / 2)
        if "end_position" in cue:
            end_anchor = _position(cue["end_position"], f"Caption {index} end_position", width, height,
                                   estimated_width / 2, estimated_height / 2)
            placement = "\\move(" + ",".join(_pixel(v) for v in (*anchor, *end_anchor)) + ")"
        else:
            end_anchor = None
            placement = "\\pos(" + ",".join(_pixel(v) for v in anchor) + ")"
        selected = style["accent"] if cue.get("accent") or cue.get("color") == "accent" else style["primary"]
        fade = min(round(style["fade_ms"]), round((end - start) * 1000 / 4))
        overrides = "{\\an5" + placement + f"\\c&H{selected}&\\fad({fade},{fade})" + "}"
        events.append(f"Dialogue: 0,{_timestamp(start, True)},{_timestamp(end, True)},Short,,0,0,0,,{overrides}{_ass_escape(text)}")
        reviewed.append({**cue, "start": start, "end": end, "text": text, "layout": {
            "mode": "near_action", "alignment": "center", "position_pixels": list(anchor),
            "end_position_pixels": list(end_anchor) if end_anchor else None,
            "estimated_safe_width": estimated_width, "estimated_safe_height": estimated_height,
            "automatic_tracking": False, "real_frame_occlusion_review_required": True}})
        previous_end, previous_export_end = end, export_end
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Short,{style['font']},{style['font_size']:g},&H00{style['primary']},&H00{style['accent']},&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,{style['outline']:g},{style['shadow']:g},5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "captions.ass").write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    write_srt(reviewed, directory / "captions.srt")
    return reviewed
