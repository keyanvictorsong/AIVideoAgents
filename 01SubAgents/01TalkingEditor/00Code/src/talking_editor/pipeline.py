"""Reusable orchestration with source fingerprints, caches and reviewable artifacts."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .captions import build_captions, write_ass, write_srt
from .planner import build_plan

SUPPORTED_VIDEO = {".mov", ".mp4", ".m4v", ".mkv", ".webm", ".avi"}
DEFAULT_CONFIG = {"backend": "auto", "model": None, "language": "zh", "planner": {},
                  "captions": {}, "render": {"preset": "fast", "threads": 4, "crf": 20},
                  "clean_video": True}


def resolve_config(config: dict | None = None) -> dict:
    if config is not None and not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in (config or {}).items():
        if key not in DEFAULT_CONFIG:
            raise ValueError(f"Unknown configuration key: {key}")
        if isinstance(merged[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f"Configuration '{key}' must be an object")
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def _write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        json.dump(data, temp, ensure_ascii=False, indent=2, allow_nan=False)
        temp.write("\n")
        temporary = Path(temp.name)
    os.replace(temporary, path)


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest_json(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _validate_reviewed_plan(plan: dict, media_info: dict) -> None:
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ValueError("Reviewed plan must be a schema_version=1 object")
    duration = media_info["duration"]
    if not isinstance(plan.get("source_duration"), (float, int)) or not math.isfinite(plan["source_duration"]) or abs(plan["source_duration"] - duration) > 0.05:
        raise ValueError("Reviewed plan source_duration does not match the video")
    if not isinstance(plan.get("keep"), list) or not plan["keep"]:
        raise ValueError("Reviewed plan must contain nonempty keep intervals")
    cursor = previous_end = 0.0
    for keep in plan["keep"]:
        try:
            a, b, o, e = (float(keep[key]) for key in ("source_start", "source_end", "output_start", "output_end"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Reviewed keep intervals need numeric source/output start/end") from exc
        if not all(math.isfinite(value) for value in (a, b, o, e)) or not 0 <= a < b <= duration + 1e-6:
            raise ValueError("Reviewed keep interval lies outside the source")
        if a < previous_end - 1e-6 or abs(o - cursor) > 1e-6 or abs((e - o) - (b - a)) > 1e-6:
            raise ValueError("Reviewed keep intervals must be ordered and map to a contiguous timeline")
        previous_end, cursor = b, e
    planned_duration = plan.get("output_duration")
    if not isinstance(planned_duration, (float, int)) or not math.isfinite(planned_duration) or abs(planned_duration - cursor) > 1e-6:
        raise ValueError("Reviewed plan output_duration does not match its keep intervals")


def edit_video(source: str | Path, output_dir: str | Path, *, config: dict | None = None,
               overwrite: bool = False, retranscribe: bool = False, analyze_only: bool = False,
               plan_path: str | Path | None = None, progress: Callable[[str], None] | None = None) -> dict:
    """Edit one video into output_dir; no shell, cloud upload, or source modification.

    Transcript results are reusable across caption/style changes. Identical completed
    runs are skipped; different requests require overwrite=True. A reviewed edit-plan
    can be rendered with plan_path (source content must match).
    """
    from . import media, speech
    emit = progress or (lambda message: None)
    started = time.monotonic()
    source, output_dir = Path(source).expanduser().resolve(), Path(output_dir).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in SUPPORTED_VIDEO:
        raise ValueError(f"Not a supported video: {source}")
    if output_dir == source.parent or output_dir == source:
        raise ValueError("Choose a separate output folder for generated files")
    config = resolve_config(config)
    source_hash = _digest_file(source)
    reviewed_plan = json.loads(Path(plan_path).read_text(encoding="utf-8")) if plan_path else None
    if reviewed_plan is not None and not isinstance(reviewed_plan, dict):
        raise ValueError("Reviewed plan must be a JSON object")
    request = {"version": __version__, "source_sha256": source_hash, "config": config,
               "reviewed_plan_sha256": _digest_json(reviewed_plan) if reviewed_plan is not None else None}
    request_hash = _digest_json(request)
    report_path = output_dir / "report.json"
    if report_path.exists():
        previous = json.loads(report_path.read_text(encoding="utf-8"))
        products = [output_dir / name for name in previous.get("artifacts", [])]
        if not retranscribe and not analyze_only and previous.get("request_sha256") == request_hash and previous.get("status") == "complete" and products and all(p.is_file() for p in products):
            emit("Already complete; using existing outputs")
            return {**previous, "skipped": True}
    if (output_dir / "edited.mp4").exists() and not overwrite:
        raise FileExistsError(f"Output already exists; use --overwrite to replace generated output: {output_dir}")
    if reviewed_plan is not None and reviewed_plan.get("source_sha256") != source_hash:
        raise ValueError("The reviewed edit plan belongs to a different source video")
    output_dir.mkdir(parents=True, exist_ok=True)
    media_info = media.probe(source)
    if reviewed_plan is not None:
        _validate_reviewed_plan(reviewed_plan, media_info)
    if not media_info["has_audio"]:
        raise ValueError("Video has no audio track; TalkingEditor needs speech audio")
    emit(f"Video: {media_info['duration']:.1f}s, {media_info['width']}×{media_info['height']}")
    asr_request = {"version": __version__, "source_sha256": source_hash,
                   "backend": config["backend"], "model": config["model"], "language": config["language"],
                   "vad": speech.VAD_CONFIG, "initial_prompt": speech.INITIAL_PROMPT_ZH}
    asr_key = _digest_json(asr_request)
    transcript_path = output_dir / "transcript.json"
    transcript = None
    if transcript_path.exists() and not retranscribe:
        cached = json.loads(transcript_path.read_text(encoding="utf-8"))
        if cached.get("cache_key") == asr_key:
            transcript = cached
            emit("Reusing original transcript and speech regions")
    if transcript is None:
        cache_dir = output_dir / ".cache"
        cache_dir.mkdir(exist_ok=True)
        audio_path = cache_dir / "analysis.wav"
        emit("Extracting analysis audio")
        media.extract_audio(source, audio_path)
        transcript = speech.transcribe(audio_path, backend=config["backend"], model=config["model"],
                                       language=config["language"], progress=emit)
        transcript.update({"cache_key": asr_key, "source_sha256": source_hash, "source": str(source)})
        _write_json(transcript_path, transcript)
    emit("Planning pauses and filler edits with continuity constraints")
    plan = reviewed_plan or build_plan(transcript, media_info["duration"], config["planner"])
    plan.update({"source": str(source), "source_sha256": source_hash, "source_media": media_info})
    _write_json(output_dir / "edit-plan.json", plan)
    cues = build_captions(transcript, plan, config["captions"])
    write_srt(cues, output_dir / "captions.srt")
    write_ass(cues, output_dir / "captions.ass", media_info["width"], media_info["height"], config["captions"])
    _write_json(output_dir / "captions.json", cues)
    _write_json(output_dir / "settings.json", config)
    fillers = plan.get("fillers", [])
    warnings = list(dict.fromkeys(transcript.get("warnings", []) + plan.get("warnings", []) + [
        "Automatic keywords use local TF-IDF and number matching; check names, facts, and subtitle text before publication.",
        "Continuity checks use timing and speech-region rules; perceptual audio/visual quality needs human preview."
    ]))
    report = {"schema_version": 1, "status": "analyzed" if analyze_only else "rendering",
              "created_at": datetime.now(timezone.utc).isoformat(), "version": __version__,
              "source": str(source), "output_dir": str(output_dir), "source_sha256": source_hash,
              "request_sha256": request_hash, "backend": transcript["backend"], "model": transcript["model"],
              "title": str(config["captions"].get("title", "")).strip(),
              "source_duration": media_info["duration"], "planned_duration": plan["output_duration"],
              "removed_seconds": round(media_info["duration"] - plan["output_duration"], 6),
              "cuts": len(plan.get("removed", [])), "caption_count": len(cues),
              "fillers_hidden": sum(bool(f.get("subtitle_hidden")) for f in fillers),
              "fillers_audio_removed": sum(bool(f.get("audio_removed")) for f in fillers),
              "fillers_audio_preserved": sum(bool(f.get("subtitle_hidden")) and not f.get("audio_removed") for f in fillers),
              "warnings": warnings, "artifacts": ["transcript.json", "edit-plan.json", "captions.srt", "captions.ass", "captions.json", "settings.json"]}
    _write_json(report_path, report)
    if not analyze_only:
        render_options = {**config["render"], "overwrite": overwrite}
        if config["clean_video"]:
            emit(f"Rendering clean edit ({plan['output_duration']:.1f}s)")
            clean = media.render(source, output_dir / "clean.mp4", plan["keep"], options=render_options)
            emit("Rendering captions and keyword emphasis")
            rendered = media.burn_subtitles(output_dir / "clean.mp4", output_dir / "edited.mp4", output_dir / "captions.ass", options=render_options)
            report["clean_media"] = clean
            report["artifacts"].append("clean.mp4")
        else:
            emit(f"Rendering edit with captions ({plan['output_duration']:.1f}s)")
            rendered = media.render(source, output_dir / "edited.mp4", plan["keep"], ass_path=output_dir / "captions.ass", options=render_options)
        report.update({"status": "complete", "output_media": rendered, "elapsed_seconds": round(time.monotonic() - started, 2)})
        report["artifacts"].append("edited.mp4")
        _write_json(report_path, report)
    emit(f"{report['status']}: {media_info['duration']:.1f}s → {plan['output_duration']:.1f}s; {len(cues)} captions")
    return report


def edit_folder(source_dir: str | Path, output_dir: str | Path, *, config: dict | None = None,
                progress: Callable[[str], None] | None = None, **kwargs) -> dict:
    """Process each direct child video independently and write a batch manifest."""
    source_dir, output_dir = Path(source_dir).expanduser().resolve(), Path(output_dir).expanduser().resolve()
    if not source_dir.is_dir():
        raise ValueError(f"Input folder does not exist: {source_dir}")
    if output_dir == source_dir:
        raise ValueError("Input and output folders must differ")
    inputs = sorted(p for p in source_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_VIDEO)
    if not inputs:
        raise ValueError(f"No supported videos found in {source_dir}")
    stems = [p.stem.casefold() for p in inputs]
    if len(stems) != len(set(stems)):
        raise ValueError("Input video names must have unique stems to avoid output collisions")
    emit = progress or (lambda message: None)
    results = []
    for index, source in enumerate(inputs, 1):
        emit(f"[{index}/{len(inputs)}] {source.name}")
        try:
            results.append(edit_video(source, output_dir / source.stem, config=config,
                                      progress=lambda message: emit(f"  {message}"), **kwargs))
        except Exception as exc:
            emit(f"  Failed: {exc}")
            results.append({"source": str(source), "status": "failed", "error": str(exc)})
    manifest = {"schema_version": 1, "source_dir": str(source_dir), "output_dir": str(output_dir),
                "results": results, "failed": sum(r["status"] == "failed" for r in results)}
    _write_json(output_dir / "batch-report.json", manifest)
    return manifest
