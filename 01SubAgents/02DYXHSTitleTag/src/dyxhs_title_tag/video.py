"""Local media evidence extraction, with macOS Vision OCR and optional local ASR.

The module does not infer game outcomes or turn OCR text into visual facts.
Every text segment keeps its source and frame evidence. No weights are downloaded.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from pathlib import Path
import platform
import re
import shutil
import subprocess
from typing import Any

SCHEMA_VERSION = 1


class VideoError(RuntimeError):
    """Media processing failed with an actionable diagnostic."""


def _run(args: list[str], *, timeout: int = 600) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VideoError(f"Cannot run {args[0]}: {exc}") from exc
    if result.returncode:
        raise VideoError(f"{Path(args[0]).name} failed: {result.stderr[-3000:].strip()}")
    return result.stdout


def _save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe_video(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve(strict=True)
    data = json.loads(_run(["ffprobe", "-v", "error", "-print_format", "json",
                            "-show_format", "-show_streams", str(source)]))
    streams = data.get("streams", [])
    videos = [item for item in streams if item.get("codec_type") == "video"
              and not item.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise VideoError("Input contains no video stream")
    duration = float(data.get("format", {}).get("duration") or videos[0].get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise VideoError("Input has no finite positive duration")
    return {
        "duration": duration, "width": videos[0].get("width"),
        "height": videos[0].get("height"), "video_streams": videos,
        "audio_streams": [item for item in streams if item.get("codec_type") == "audio"],
        "subtitle_streams": [item for item in streams if item.get("codec_type") == "subtitle"],
        "raw_probe": data,
    }


def extract_frames(video_path: str | Path, output_dir: str | Path, *,
                   interval: float = 2.0, max_width: int = 1080) -> list[dict[str, Any]]:
    """Sample original presentation times, including frame zero, without copying video."""
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("frame interval must be finite and greater than zero")
    if max_width < 16:
        raise ValueError("max_width must be at least 16")
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    # frame_pts plus a millisecond encoder time base preserves source timing,
    # including variable frame rate and long pauses between frames.
    vf = (f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval:.9f})',"
          f"scale='min({int(max_width)},iw)':-2")
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i",
          str(Path(video_path).resolve()), "-map", "0:v:0", "-vf", vf,
          "-fps_mode", "vfr", "-enc_time_base", "1:1000", "-frame_pts", "1",
          "-q:v", "3", str(destination / "pts_%012d.jpg")])
    frames = []
    for index, path in enumerate(sorted(destination.glob("pts_*.jpg")), 1):
        frames.append({"id": f"frame-{index:05d}",
                       "timestamp_seconds": int(path.stem.removeprefix("pts_")) / 1000,
                       "timestamp_precision_seconds": 0.001,
                       "path": str(path)})
    if not frames:
        raise VideoError("ffmpeg returned no extracted frames")
    return frames


def _vision_ocr(frames: list[dict[str, Any]], work_dir: Path) -> list[dict[str, Any]]:
    source = Path(__file__).with_name("vision_ocr.swift")
    executable = work_dir / "vision-ocr"
    if not executable.exists() or executable.stat().st_mtime < source.stat().st_mtime:
        _run(["swiftc", str(source), "-o", str(executable)], timeout=180)
    raw = _run([str(executable), *(item["path"] for item in frames)], timeout=900)
    (work_dir / "ocr_raw.jsonl").write_text(raw, encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _tesseract_ocr(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    languages = set(_run(["tesseract", "--list-langs"]).splitlines())
    if "chi_sim" not in languages:
        raise VideoError("Chinese OCR unavailable: use macOS Vision or install tesseract chi_sim data")
    records = []
    for frame in frames:
        rows = csv.DictReader(io.StringIO(_run(["tesseract", frame["path"], "stdout",
                                               "-l", "chi_sim+eng", "tsv"])), delimiter="\t")
        grouped: dict[tuple[str, str, str], list[dict[str, str]]] = {}
        for row in rows:
            if row.get("text", "").strip() and float(row.get("conf", -1)) >= 0:
                key = (row.get("block_num", ""), row.get("par_num", ""), row.get("line_num", ""))
                grouped.setdefault(key, []).append(row)
        lines = [{"text": " ".join(row["text"] for row in group),
                  "confidence": sum(float(row["conf"]) for row in group) / (100 * len(group))}
                 for group in grouped.values()]
        records.append({"path": frame["path"], "engine": "tesseract", "lines": lines})
    return records


def _normalise_text(text: str) -> str:
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).casefold()


def merge_ocr_segments(frames: list[dict[str, Any]], records: list[dict[str, Any]],
                       duration: float, interval: float) -> list[dict[str, Any]]:
    """Merge only adjacent identical observations, retaining all frame IDs.

    Times are sampling coverage, not word-level alignment. Empty frames break a
    segment; the same phrase later in the video remains separate evidence.
    """
    by_path = {str(Path(item["path"]).resolve()): item for item in records}
    result: list[dict[str, Any]] = []
    previous_frame_had_text = False
    for index, frame in enumerate(frames):
        record = by_path.get(str(Path(frame["path"]).resolve()), {})
        lines = [line for line in record.get("lines", []) if str(line.get("text", "")).strip()]
        text = " ".join(str(line["text"]).strip() for line in lines)
        start = max(0.0, float(frame["timestamp_seconds"]))
        end = min(duration, float(frames[index + 1]["timestamp_seconds"])) if index + 1 < len(frames) else min(duration, start + interval)
        if not text:
            previous_frame_had_text = False
            continue
        confidence = sum(float(line.get("confidence", 0)) for line in lines) / len(lines)
        if (result and previous_frame_had_text
                and _normalise_text(result[-1]["text"]) == _normalise_text(text)
                and start - result[-1]["end"] <= interval * 0.1):
            result[-1]["end"] = round(end, 3)
            result[-1]["frame_ids"].append(frame["id"])
            result[-1]["confidence"] = min(result[-1]["confidence"], round(confidence, 4))
        else:
            result.append({"id": f"ocr-{len(result) + 1:05d}", "start": round(start, 3),
                           "end": round(end, 3), "text": text,
                           "confidence": round(confidence, 4),
                           "source": record.get("engine", "ocr"),
                           "frame_ids": [frame["id"]], "timing": "sample_coverage"})
        previous_frame_had_text = True
    return result


def parse_subtitles(text: str, *, source: str = "subtitle") -> list[dict[str, Any]]:
    """Parse SRT/WebVTT cue timing; formatting and cue identifiers are discarded."""
    timing = re.compile(r"(?P<a>(?:\d+:)?\d{2}:\d{2}[,.]\d{3})\s*-->\s*(?P<b>(?:\d+:)?\d{2}:\d{2}[,.]\d{3})")
    def seconds(value: str) -> float:
        parts = value.replace(",", ".").split(":")
        return sum(float(piece) * 60 ** index for index, piece in enumerate(reversed(parts)))
    segments = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = block.splitlines()
        for index, line in enumerate(lines):
            match = timing.search(line)
            if match:
                content = re.sub(r"<[^>]+>", "", " ".join(lines[index + 1:])).strip()
                start, end = seconds(match["a"]), seconds(match["b"])
                if content and end > start:
                    segments.append({"id": f"subtitle-{len(segments) + 1:05d}",
                                     "start": start, "end": end, "text": content,
                                     "confidence": None, "source": source, "frame_ids": [],
                                     "timing": "subtitle_cue"})
                break
    return segments


def _extract_subtitles(video: Path, metadata: dict[str, Any], destination: Path) -> list[dict[str, Any]]:
    for extension in (".srt", ".vtt"):
        sidecar = video.with_suffix(extension)
        if sidecar.is_file():
            found = parse_subtitles(sidecar.read_text(encoding="utf-8-sig"), source=str(sidecar))
            if found:
                return found
    text_codecs = {"mov_text", "subrip", "webvtt", "ass", "ssa", "text"}
    for stream in metadata["subtitle_streams"]:
        if stream.get("codec_name") in text_codecs:
            target = destination / f"stream-{stream['index']}.srt"
            _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
                  "-map", f"0:{stream['index']}", "-c:s", "srt", str(target)])
            found = parse_subtitles(target.read_text(encoding="utf-8-sig"), source="embedded_subtitle")
            if found:
                return found
    return []


def transcribe_audio(video: Path, destination: Path, *, python: str,
                     model: str, backend: str = "mlx") -> list[dict[str, Any]]:
    """Run an existing local recognizer in its own Python environment.

    A local model path is required, so invoking this function cannot silently
    download large weights. Recognition is not independent speech detection.
    """
    model_path = Path(model).expanduser().resolve()
    if not model_path.exists():
        raise VideoError("ASR requires an existing local model path; no model will be downloaded")
    audio = destination / "audio.wav"
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
          "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)])
    worker = Path(__file__).with_name("asr_worker.py")
    raw = _run([python, str(worker), str(audio), str(model_path), backend], timeout=1800)
    data = json.loads(raw)
    _save(destination / "asr.json", data)
    segments = []
    for row in data.get("segments", []):
        content = str(row.get("text", "")).strip()
        start, end = float(row.get("start", 0)), float(row.get("end", 0))
        # Silence/no-speech candidates must never become affirmative evidence.
        if (not content or not math.isfinite(start) or not math.isfinite(end)
                or end <= start or float(row.get("no_speech_prob", 0)) > 0.6):
            continue
        segments.append({"id": f"asr-{len(segments) + 1:05d}", "start": start,
                         "end": end, "text": content, "confidence": None,
                         "source": f"asr:{backend}", "frame_ids": [],
                         "timing": "asr_segment", "review_required": True})
    return segments


def analyze_video(video_path: str | Path, output_dir: str | Path, *,
                  frame_interval: float = 2.0, ocr: bool = True,
                  asr: bool = False, asr_python: str | None = None,
                  asr_model: str | None = None, asr_backend: str = "mlx",
                  refresh: bool = False) -> dict[str, Any]:
    """Extract reusable evidence and save analysis JSON plus a timed transcript."""
    if not math.isfinite(frame_interval) or frame_interval <= 0:
        raise ValueError("frame_interval must be finite and greater than zero")
    video = Path(video_path).expanduser().resolve(strict=True)
    if not video.is_file():
        raise VideoError("Input must be a video file")
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    digest = file_sha256(video)
    config = {"frame_interval": frame_interval, "ocr": ocr, "asr": asr,
              "asr_python": asr_python, "asr_model": asr_model, "asr_backend": asr_backend,
              "schema_version": SCHEMA_VERSION}
    config["sidecars"] = {extension: file_sha256(video.with_suffix(extension))
                          for extension in (".srt", ".vtt") if video.with_suffix(extension).is_file()}
    result_path = destination / "video_analysis.json"
    if not refresh and result_path.is_file():
        try:
            cached = json.loads(result_path.read_text(encoding="utf-8"))
            if (cached.get("source", {}).get("sha256") == digest
                    and cached.get("config") == config
                    and all(Path(item["path"]).is_file() for item in cached.get("frames", []))):
                return cached
        except (OSError, ValueError, KeyError):
            pass
    metadata = probe_video(video)
    # Fingerprint-specific folders prevent stale files mixing with a new input.
    frames = extract_frames(video, destination / "frames" / digest[:12] / f"interval-{frame_interval:g}",
                            interval=frame_interval)
    warnings = ["OCR segment times describe sampling coverage; text between sampled frames may be missed."]
    subtitles = _extract_subtitles(video, metadata, destination)
    records: list[dict[str, Any]] = []
    if ocr:
        try:
            if platform.system() == "Darwin" and shutil.which("swiftc"):
                records = _vision_ocr(frames, destination)
            elif shutil.which("tesseract"):
                records = _tesseract_ocr(frames)
            else:
                raise VideoError("No supported OCR engine found (macOS swiftc/Vision or tesseract chi_sim)")
            errors = [row.get("error") for row in records if row.get("error")]
            warnings.extend(f"OCR failed for a frame: {error}" for error in errors)
            _save(destination / "ocr.json", records)
        except VideoError as exc:
            warnings.append(str(exc))
    ocr_segments = merge_ocr_segments(frames, records, metadata["duration"], frame_interval)
    speech = []
    if asr:
        if not metadata["audio_streams"]:
            warnings.append("ASR skipped: video has no audio track")
        elif not asr_python or not asr_model:
            warnings.append("ASR skipped: configure asr_python and an existing local asr_model path")
        else:
            try:
                speech = transcribe_audio(video, destination, python=asr_python,
                                          model=asr_model, backend=asr_backend)
                warnings.append("ASR output requires review; speech activity was not independently verified.")
            except VideoError as exc:
                warnings.append(str(exc))
    else:
        warnings.append("ASR not requested; audio contents and presence of speech have not been verified.")
    text_segments = subtitles or ocr_segments or speech
    evidence = subtitles + ocr_segments + speech
    if not evidence:
        warnings.append("No text evidence was extracted. Visual review or a configured vision model is required.")
    transcript = "\n".join(f"[{item['start']:.3f}–{item['end']:.3f}] {item['text']}" for item in text_segments)
    transcript_path = destination / "transcript.txt"
    transcript_path.write_text(transcript + ("\n" if transcript else ""), encoding="utf-8")
    result = {"schema_version": SCHEMA_VERSION,
              "source": {"path": str(video), "sha256": digest, "size_bytes": video.stat().st_size},
              "config": config, "metadata": metadata, "frames": frames,
              "subtitles": subtitles or ocr_segments, "ocr_segments": ocr_segments,
              "asr_segments": speech, "transcript": transcript, "evidence": evidence,
              "warnings": warnings, "status": "ok" if evidence else "visual_review_required",
              "artifact_paths": {"analysis_json": str(result_path), "ocr_json": str(destination / "ocr.json") if records else None,
                                 "transcript_txt": str(transcript_path)}}
    _save(result_path, result)
    return result
