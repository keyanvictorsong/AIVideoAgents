"""Build a conservative, reversible edit plan without changing the transcript.

All timestamps are seconds in the original media. ``speech_regions`` must come
from independent voice activity detection (VAD), never from gaps in ASR words.
Words supplement VAD to protect speech the detector missed. A zero-duration
word protects its entire overlapping ASR segment because its actual boundaries
are unknown; it can never supply an audio cut point. Without VAD the
original audio/video is retained, while obvious filler words can still be
hidden in subtitles.

Defaults favor natural speech: internal pauses over .9s retain .45s; leading and
trailing silence trim to .14s speech padding; kept fragments must be at least
.75s; a ten-second source window
can contain at most three internal cuts. Filler audio removal requires .85
word confidence and an isolated VAD island with silent boundaries. Ambiguous
words such as 那个, 这个, 就是, and 然后 are deliberately never auto-deleted.
"""

from __future__ import annotations

import math
import unicodedata
from typing import Any


DEFAULTS = {
    "max_pause": 0.9,
    "keep_pause": 0.45,
    "speech_padding": 0.14,
    "min_keep": 0.75,
    "min_filler_confidence": 0.85,
    "max_cuts_per_window": 3,
    "cut_window": 10.0,
    "remove_fillers": True,
    "remove_filler_audio": True,
}

_FILLERS = frozenset({"嗯", "嗯嗯", "呃", "呃呃", "uh", "uhh", "um", "umm", "erm", "hmm"})
_EPS = 1e-9
_BOUNDARY_TOLERANCE = 1e-4  # Permit demuxer/serialization rounding, not ASR overrun.
_MAX_FILLER_BOUNDARY_SLACK = 0.35


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _range(item: dict, name: str, duration: float, *, allow_zero: bool = False) -> tuple[float, float]:
    if not isinstance(item, dict) or "start" not in item or "end" not in item:
        raise ValueError(f"{name} must contain start and end")
    start = _number(item["start"], f"{name}.start")
    end = _number(item["end"], f"{name}.end")
    if start < -_BOUNDARY_TOLERANCE or end > duration + _BOUNDARY_TOLERANCE or end < start or (end == start and not allow_zero):
        inequality = "<=" if allow_zero else "<"
        raise ValueError(f"{name} must satisfy 0 <= start {inequality} end <= duration")
    start, end = max(0.0, min(duration, start)), max(0.0, min(duration, end))
    if start == end and not allow_zero:
        raise ValueError(f"{name} has no positive duration after boundary rounding")
    return start, end


def _config(config: dict | None) -> dict:
    if config is not None and not isinstance(config, dict):
        raise ValueError("config must be a dictionary or None")
    result = dict(DEFAULTS)
    result.update(config or {})
    for name in ("max_pause", "keep_pause", "speech_padding", "min_keep", "cut_window"):
        result[name] = _number(result[name], name)
        if result[name] < 0 or (name in {"max_pause", "cut_window"} and result[name] == 0):
            raise ValueError(f"{name} must be {'positive' if name in {'max_pause', 'cut_window'} else 'nonnegative'}")
    confidence = _number(result["min_filler_confidence"], "min_filler_confidence")
    if not 0 <= confidence <= 1:
        raise ValueError("min_filler_confidence must be between 0 and 1")
    result["min_filler_confidence"] = confidence
    count = result["max_cuts_per_window"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("max_cuts_per_window must be a positive integer")
    for name in ("remove_fillers", "remove_filler_audio"):
        if not isinstance(result[name], bool):
            raise ValueError(f"{name} must be a boolean")
    return result


def _merged(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1] + _EPS:
            result[-1] = result[-1][0], max(end, result[-1][1])
        else:
            result.append((start, end))
    return result


def _merged_cuts(cuts: list[dict]) -> list[dict]:
    result: list[dict] = []
    for cut in sorted(cuts, key=lambda c: (c["start"], c["end"])):
        if result and cut["start"] <= result[-1]["end"] + _EPS:
            result[-1]["end"] = max(result[-1]["end"], cut["end"])
            result[-1]["reasons"].update(cut["reasons"])
        else:
            result.append({"start": cut["start"], "end": cut["end"], "reasons": set(cut["reasons"])})
    return result


def _keep(cuts: list[dict], duration: float) -> list[dict]:
    result = []
    source_cursor = output_cursor = 0.0
    for cut in [*cuts, {"start": duration, "end": duration}]:
        if cut["start"] > source_cursor + _EPS:
            length = cut["start"] - source_cursor
            result.append({
                "source_start": source_cursor,
                "source_end": cut["start"],
                "output_start": output_cursor,
                "output_end": output_cursor + length,
            })
            output_cursor += length
        source_cursor = cut["end"]
    return result


def _constraint_failure(cuts: list[dict], duration: float, config: dict) -> str | None:
    keeps = _keep(cuts, duration)
    if not keeps:
        return "would remove the entire video"
    if any(k["source_end"] - k["source_start"] < config["min_keep"] - _EPS for k in keeps):
        return "would leave an unnaturally short kept fragment"
    # Trimming the start/end creates no jump between two spoken passages.
    centers = sorted((c["start"] + c["end"]) / 2 for c in cuts if c["start"] > 0 and c["end"] < duration)
    limit = config["max_cuts_per_window"]
    if any(centers[i + limit] - centers[i] < config["cut_window"] for i in range(len(centers) - limit)):
        return "would create too many cuts close together"
    return None


def _token(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return "".join(c for c in normalized if not c.isspace() and unicodedata.category(c)[0] not in {"P", "S"})


def _filler_groups(words: list[dict], config: dict) -> list[dict]:
    groups: list[list[dict]] = []
    previous_was_filler = False
    for word in words:
        is_filler = _token(word["text"]) in _FILLERS
        if is_filler:
            if previous_was_filler and word["start"] - groups[-1][-1]["end"] <= config["max_pause"]:
                groups[-1].append(word)
            else:
                groups.append([word])
        previous_was_filler = is_filler
    return [{
        "word_ids": [w["id"] for w in group],
        "text": "".join(w["text"] for w in group),
        "start": group[0]["start"],
        "end": max(w["end"] for w in group),
        "subtitle_hidden": config["remove_fillers"],
        "audio_removed": False,
        "reason": "filler removal disabled" if not config["remove_fillers"] else "subtitle cleaned; audio retained for fluent speech",
    } for group in groups]


def _filler_cut(group: dict, words: list[dict], vad: list[tuple[float, float]],
                speech: list[tuple[float, float]], duration: float, config: dict) -> tuple[dict | None, str]:
    ids = set(group["word_ids"])
    filler_words = [w for w in words if w["id"] in ids]
    if any(w["end"] <= w["start"] for w in filler_words):
        return None, "subtitle cleaned; audio retained because filler word has no reliable duration"
    if any(w.get("probability") is None or w["probability"] < config["min_filler_confidence"] for w in filler_words):
        return None, "subtitle cleaned; audio retained because filler timing/confidence is uncertain"
    if not any(start < group["end"] and end > group["start"] for start, end in vad):
        return None, "subtitle cleaned; audio retained because independent VAD did not confirm the filler"
    indexes = [i for i, (start, end) in enumerate(speech) if start < group["end"] and end > group["start"]]
    first, last = indexes[0], indexes[-1]
    start, end = speech[first][0], speech[last][1]
    if start < group["start"] - _MAX_FILLER_BOUNDARY_SLACK or end > group["end"] + _MAX_FILLER_BOUNDARY_SLACK:
        return None, "subtitle cleaned; audio retained because the VAD region contains additional speech"
    if any(w["id"] not in ids and w["start"] < end and w["end"] > start for w in words):
        return None, "subtitle cleaned; audio retained because filler shares a speech region with useful words"
    previous_end = speech[first - 1][1] if first else 0.0
    next_start = speech[last + 1][0] if last + 1 < len(speech) else duration
    left_gap, right_gap = start - previous_end, next_start - end
    padding = config["speech_padding"]
    separation = max(padding * 2, 0.12)
    if (first and left_gap < separation) or (last + 1 < len(speech) and right_gap < separation):
        return None, "subtitle cleaned; audio retained because there is insufficient silence for a smooth cut"
    cut_start = max(previous_end + padding, start - min(left_gap / 2, config["keep_pause"] / 2)) if first else 0.0
    cut_end = min(next_start - padding, end + min(right_gap / 2, config["keep_pause"] / 2)) if last + 1 < len(speech) else duration
    return {"start": cut_start, "end": cut_end, "reasons": {"filler"}}, "removed filler with adjacent silence"


def build_plan(transcript: dict, duration: float, config: dict | None = None) -> dict:
    """Return source/output keeps, removed ranges, and auditable filler decisions.

    Invalid or out-of-media timestamps raise ``ValueError``. Boundary rounding
    within 1e-4s is clamped on local copies; the input is never mutated. Each
    kept source range maps at speed 1 to a contiguous output range.
    A missing or empty ``speech_regions`` list yields an unchanged video because
    omitted ASR text is not evidence of silence.
    """
    duration = _number(duration, "duration")
    if duration <= 0:
        raise ValueError("duration must be positive")
    if not isinstance(transcript, dict):
        raise ValueError("transcript must be a dictionary")
    cfg = _config(config)
    raw_words = transcript.get("words", [])
    if not isinstance(raw_words, list):
        raise ValueError("words must be a list")
    words = []
    ids: set[int] = set()
    for index, word in enumerate(raw_words):
        start, end = _range(word, f"words[{index}]", duration, allow_zero=True)
        identifier = word.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier in ids:
            raise ValueError("word ids must be unique integers")
        ids.add(identifier)
        if not isinstance(word.get("text"), str):
            raise ValueError("word text must be a string")
        if word.get("probability") is not None:
            probability = _number(word["probability"], f"words[{index}].probability")
            if not 0 <= probability <= 1:
                raise ValueError("word probability must be between 0 and 1")
        words.append({**word, "start": start, "end": end})
    words = sorted(words, key=lambda w: (w["start"], w["end"], w["id"]))
    segments = transcript.get("segments", [])
    if not isinstance(segments, list):
        raise ValueError("segments must be a list")
    segment_ranges = [_range(segment, f"segments[{index}]", duration, allow_zero=True) for index, segment in enumerate(segments)]
    regions = transcript.get("speech_regions")
    if regions is not None and not isinstance(regions, list):
        raise ValueError("speech_regions must be a list")
    vad = [_range(region, f"speech_regions[{index}]", duration) for index, region in enumerate(regions or [])]
    fillers = _filler_groups(words, cfg)
    warnings: list[str] = []
    candidates: list[dict] = []
    filler_proposals: dict[int, dict] = {}
    zero_words = [w for w in words if w["start"] == w["end"]]
    protected_segments: list[tuple[float, float]] = []
    unmatched_zero_words = False
    for word in zero_words:
        matches = [(start, end) for start, end in segment_ranges if start < end and start <= word["start"] <= end]
        protected_segments.extend(matches)
        unmatched_zero_words = unmatched_zero_words or not matches
    if zero_words:
        warnings.append("Zero-duration ASR words have uncertain boundaries; their full ASR segments are protected from audio/video edits.")
    if unmatched_zero_words:
        warnings.append("A zero-duration ASR word has no containing segment; original audio/video retained to avoid cutting uncertain speech.")
    if not vad:
        warnings.append("No independent VAD speech regions available; original audio/video retained. ASR gaps are not treated as silence.")
        for filler in fillers:
            if cfg["remove_fillers"]:
                filler["reason"] = "subtitle cleaned; audio retained because independent VAD is unavailable"
    elif not unmatched_zero_words:
        speech = _merged(vad + [(w["start"], w["end"]) for w in words if w["end"] > w["start"]] + protected_segments)
        padding = cfg["speech_padding"]
        if speech[0][0] > padding + _EPS:
            candidates.append({"start": 0.0, "end": max(0.0, speech[0][0] - padding), "reasons": {"leading_silence"}})
        for (_, previous_end), (next_start, _) in zip(speech, speech[1:]):
            gap = next_start - previous_end
            retained = max(cfg["keep_pause"], padding * 2)
            if gap > cfg["max_pause"] and gap > retained:
                candidates.append({"start": previous_end + retained / 2, "end": next_start - retained / 2, "reasons": {"long_pause"}})
        if duration - speech[-1][1] > padding + _EPS:
            candidates.append({"start": min(duration, speech[-1][1] + padding), "end": duration, "reasons": {"trailing_silence"}})
        if cfg["remove_fillers"] and not cfg["remove_filler_audio"]:
            for filler in fillers:
                filler["reason"] = "subtitle cleaned; filler audio removal disabled"
        if cfg["remove_fillers"] and cfg["remove_filler_audio"]:
            for index, filler in enumerate(fillers):
                proposal, reason = _filler_cut(filler, words, vad, speech, duration, cfg)
                if proposal and any(proposal["start"] < end and proposal["end"] > start for start, end in protected_segments):
                    proposal = None
                    reason = "subtitle cleaned; audio retained to protect the full ASR segment around a zero-duration word"
                filler["reason"] = reason
                if proposal:
                    candidates.append(proposal)
                    filler_proposals[index] = proposal
    # Prefer larger useful removals. Every accepted candidate is checked against
    # the complete plan so later cuts cannot create tiny spoken fragments.
    accepted: list[dict] = []
    rejected = 0
    for candidate in sorted(_merged_cuts(candidates), key=lambda c: (-(c["end"] - c["start"]), c["start"])):
        trial = _merged_cuts([*accepted, candidate])
        failure = _constraint_failure(trial, duration, cfg)
        if failure:
            rejected += 1
        else:
            accepted = trial
    if rejected:
        warnings.append(f"Retained {rejected} proposed removal(s) to avoid short fragments or excessive cuts.")
    for index, proposal in filler_proposals.items():
        filler = fillers[index]
        filler["audio_removed"] = any(c["start"] <= filler["start"] + _EPS and c["end"] >= filler["end"] - _EPS for c in accepted)
        if not filler["audio_removed"]:
            filler["reason"] = "subtitle cleaned; audio retained to avoid short fragments or excessive cuts"
    keep = _keep(accepted, duration)
    return {
        "schema_version": 1,
        "source_duration": duration,
        "output_duration": keep[-1]["output_end"] if keep else 0.0,
        "keep": keep,
        "removed": [{"start": c["start"], "end": c["end"], "reason": "+".join(sorted(c["reasons"]))} for c in accepted],
        "fillers": fillers,
        "warnings": warnings,
    }
