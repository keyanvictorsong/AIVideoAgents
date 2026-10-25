"""Resumable one-video delivery for the chat agent's title/cover workflow.

Speech analysis and rendering are local. Title selection and image generation
belong to the calling agent; this module does not silently call a paid API.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil
import sys

from . import media
from .pipeline import SUPPORTED_VIDEO, _digest_file, _write_json, edit_video


PUBLICATION_COVERS = {"vertical": (1080, 1440, "3:4"), "horizontal": (1440, 1080, "4:3")}


def _paths(source: str | Path, output: str | Path | None = None) -> tuple[Path, Path]:
    source = Path(source).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in SUPPORTED_VIDEO:
        raise ValueError(f"Expected one video file: {source}")
    output = Path(output).expanduser().resolve() if output else source.with_name(source.stem + "_output")
    if output == source or output == source.parent:
        raise ValueError("Use a separate output folder")
    fingerprint = _digest_file(source)
    for filename in ("delivery.json", "report.json"):
        path = output / filename
        if path.exists() and _read(path).get("source_sha256") != fingerprint:
            raise ValueError("Existing output belongs to different source content; choose --output with a new folder")
    return source, output


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _title(output: Path) -> str:
    settings = output / "settings.json"
    return str(_read(settings).get("captions", {}).get("title", "")).strip() if settings.exists() else ""


def _covers_unchanged(output: Path, previous: dict) -> bool:
    """Keep legacy jobs resumable while requiring both recorded publication images."""
    if "covers" not in previous:
        cover = output / "cover.png"
        return cover.is_file() and previous.get("cover_sha256") == _digest_file(cover)
    covers = previous["covers"]
    if not isinstance(covers, dict) or set(covers) != set(PUBLICATION_COVERS):
        return False
    for role, (width, height, ratio) in PUBLICATION_COVERS.items():
        item = covers[role]
        if not isinstance(item, dict):
            return False
        name = item.get("image")
        if name not in (f"cover-{role}.png", f"cover-{role}.jpg"):
            return False
        path = output / name
        if (item.get("width"), item.get("height"), item.get("ratio")) != (width, height, ratio):
            return False
        if not path.is_file() or item.get("sha256") != _digest_file(path):
            return False
    return True


def _frame_time(transcript: dict, duration: float) -> float:
    target = min(duration / 2, max(0.0, duration * 0.1))
    regions = transcript.get("speech_regions", [])
    if regions:
        region = min(regions, key=lambda r: abs((r["start"] + r["end"]) / 2 - target))
        target = min(max(target, region["start"]), (region["start"] + region["end"]) / 2)
    return min(max(0.0, target), max(0.0, duration - 0.05))


def extract_cover_frame(source: Path, destination: Path, timestamp: float, *, options: dict | None = None) -> None:
    metadata = media.probe(source)
    if not math.isfinite(timestamp) or not 0 <= timestamp < metadata["duration"]:
        raise ValueError("Cover timestamp must lie within the video")
    filters = media._colour_filters(metadata, media._filters(media._binary("ffmpeg")), options)
    if destination.suffix.lower() in {".jpg", ".jpeg"}:
        # JPEG decoders use the full-range BT.601 YCbCr matrix. FFmpeg's
        # automatic format conversion can otherwise retain BT.709 samples
        # while the JPEG is decoded as BT.601, shifting the speaker's colours.
        filters += ["scale=in_color_matrix=bt709:out_color_matrix=bt601:in_range=tv:out_range=pc",
                    "format=yuvj420p",
                    "setparams=range=full:color_primaries=bt709:color_trc=bt709:colorspace=bt470bg"]
    media._run([media._binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-ss", str(timestamp), "-i", str(source), "-frames:v", "1", "-vf", ",".join(filters),
                "-map_metadata", "-1", str(destination)])


def cover_prompt(title: str, width: int, height: int) -> str:
    orientation = "portrait" if height >= width else "landscape"
    return (
        f"Use case: identity-preserve. Asset: finished Chinese talking-head video cover, {orientation}, "
        f"aspect ratio {width}:{height}. The input image is the actual speaker extracted from the video. "
        "Preserve the recognizable person's identity and the source image's factual details. "
        "Use the current user's chosen typography, palette, placement and visual style; "
        "if none was supplied, use a simple readable high-contrast layout with safe margins and an uncovered face. "
        f"Exact title text: {json.dumps(title, ensure_ascii=False)}. It may wrap into two lines without changing characters. "
        "Do not invent people or source details, or add unrelated text and logos. "
        "Output a complete PNG cover."
    )


def prepare(source: str | Path, output: str | Path | None = None, *, cover_time: float | None = None,
            progress=print) -> dict:
    """Analyze one video and prepare the source-linked brief/reference, resuming existing work."""
    source, output = _paths(source, output)
    report_path = output / "report.json"
    report = _read(report_path) if report_path.exists() else {}
    # A previously approved video must not be downgraded by analyze_only writes.
    rendered = report.get("status") == "complete" and (output / "edited.mp4").is_file()
    analysis_available = (report.get("status") in {"analyzed", "rendering"}
                          and all((output / name).is_file() for name in ("transcript.json", "edit-plan.json", "settings.json")))
    if not rendered and not analysis_available:
        report = edit_video(source, output, analyze_only=True, progress=progress)
    transcript = _read(output / "transcript.json")
    text = "\n".join(s["text"].strip() for s in transcript.get("segments", []) if s.get("text", "").strip())
    if not text:
        text = "".join(w["text"] for w in transcript.get("words", []))
    if not text.strip():
        raise ValueError("No usable speech transcript; cannot choose a factual title or create a talking-head cover")
    (output / "content.txt").write_text(text + "\n", encoding="utf-8")
    info = report.get("source_media") or media.probe(source)
    job_path = output / "delivery.json"
    previous = _read(job_path) if job_path.exists() else {}
    cover_meta = _read(output / "cover-meta.json") if (output / "cover-meta.json").exists() else {}
    timestamp = cover_time if cover_time is not None else previous.get("cover_time", cover_meta.get("source_frame_seconds"))
    if timestamp is None:
        timestamp = _frame_time(transcript, info["duration"])
    settings = _read(output / "settings.json")
    cover_options = {"tonemap_desat": media._tonemap_desat(settings.get("render", {}))}
    # Older references used the historical default. Regenerate a reference if
    # the task's colour choice changes, keeping its selected source timestamp.
    previous_options = previous.get("cover_color_options", {"tonemap_desat": 2.0})
    reference = output / "cover-reference.jpg"
    if not reference.exists() or cover_time is not None or previous_options != cover_options:
        extract_cover_frame(source, reference, timestamp, options=cover_options)
    job = {**previous, "schema_version": 1, "source": str(source), "output_dir": str(output),
           "source_sha256": report["source_sha256"], "title": _title(output),
           "status": "rendered" if rendered else "prepared", "content": "content.txt",
           "cover_reference": "cover-reference.jpg", "cover_time": timestamp, "cover_color_options": cover_options,
           "width": info["width"], "height": info["height"]}
    if (previous.get("status") == "complete" and previous.get("title") == job["title"]
            and rendered
            and all((output / p).is_file() for p in ("edited.mp4", "verification.json", *report.get("artifacts", [])))
            and previous.get("edited_sha256") == _digest_file(output / "edited.mp4")
            and _covers_unchanged(output, previous)):
        job["status"] = "complete"
    _write_json(job_path, job)
    return job


def render(source: str | Path, title: str, output: str | Path | None = None, *, keywords: list[str] | None = None,
           overwrite: bool = False, progress=print) -> dict:
    """Render a content-selected title; write the matching prompt for the agent's image tool."""
    title = title.strip()
    if not title:
        raise ValueError("Choose a nonempty title from the actual transcript")
    job = prepare(source, output, progress=progress)
    output = Path(job["output_dir"])
    settings = _read(output / "settings.json")
    settings.setdefault("captions", {})["title"] = title
    if keywords is not None:
        settings["captions"]["keywords"] = keywords
    prior_report = _read(output / "report.json")
    resuming_render = (prior_report.get("status") == "rendering"
                       or (prior_report.get("status") == "complete"
                           and any(not (output / name).is_file() for name in prior_report.get("artifacts", []))))
    report = edit_video(source, output, config=settings, overwrite=overwrite or resuming_render, progress=progress)
    prompt_path = output / "cover-prompt.txt"
    if not prompt_path.exists() or title != job["title"]:
        prompt_path.write_text(cover_prompt(title, job["width"], job["height"]) + "\n", encoding="utf-8")
    complete = report.get("skipped") and job["status"] == "complete"
    job.update({"status": "complete" if complete else "rendered", "title": title, "cover_prompt": "cover-prompt.txt",
                "removed_seconds": report["removed_seconds"], "fillers_audio_removed": report["fillers_audio_removed"]})
    _write_json(output / "delivery.json", job)
    return job


def finish(source: str | Path, output: str | Path | None = None, *, cover_source: str | Path | None = None,
           cover_title: str | None = None, cover_vertical_source: str | Path | None = None,
           cover_horizontal_source: str | Path | None = None, overwrite: bool = False) -> dict:
    """Validate video/cover provenance and decoding before marking the combined job complete.

    The calling agent visually checks identity and the actual cover lettering.
    cover_title records that declared title; this is not an OCR claim.
    Explicit publication covers must be supplied together at their exact target
    sizes. The original single PNG interface retains its video-aspect check.
    """
    publication = cover_vertical_source is not None or cover_horizontal_source is not None
    if publication and (cover_vertical_source is None or cover_horizontal_source is None):
        raise ValueError("Provide both vertical and horizontal publication covers")
    if publication and cover_source is not None:
        raise ValueError("Use either the publication cover pair or the legacy single cover")
    source, output = _paths(source, output)
    report = _read(output / "report.json")
    if report.get("status") != "complete" or not (output / "edited.mp4").is_file():
        raise ValueError("Render the titled video before finishing delivery")
    if any(not (output / name).is_file() for name in report.get("artifacts", [])):
        raise ValueError("Video delivery is missing an artifact listed by its render report")
    title = _title(output)
    if not title:
        raise ValueError("The video must have a title before finishing delivery")
    if report.get("title") is not None and report["title"] != title:
        raise ValueError("Settings title does not match the completed render; render the new title first")
    old_meta = _read(output / "cover-meta.json") if (output / "cover-meta.json").exists() else {}
    declared_title = cover_title if cover_title is not None else old_meta.get("title")
    if declared_title != title:
        raise ValueError("Cover title must match the rendered video title")
    if old_meta.get("source_sha256") not in (None, report["source_sha256"]):
        raise ValueError("Cover metadata belongs to a different source video")
    video = media.probe(output / "edited.mp4")
    # Revalidation without new paths follows the previously recorded mode.
    reuse_publication = not publication and cover_source is None and "covers" in old_meta
    publication = publication or reuse_publication
    covers = {}
    copies = []
    if publication:
        if reuse_publication and (not isinstance(old_meta["covers"], dict)
                                  or set(old_meta["covers"]) != set(PUBLICATION_COVERS)):
            raise ValueError("Missing recorded vertical or horizontal publication cover")
        provided = {"vertical": cover_vertical_source, "horizontal": cover_horizontal_source}
        for role, (width, height, ratio) in PUBLICATION_COVERS.items():
            if reuse_publication:
                recorded = old_meta.get("covers", {}).get(role, {})
                if not isinstance(recorded, dict):
                    raise ValueError(f"Missing recorded {role} publication cover")
                name = recorded.get("image")
                if name not in (f"cover-{role}.png", f"cover-{role}.jpg"):
                    raise ValueError(f"Missing recorded {role} publication cover")
                cover = output / name
            else:
                cover = Path(provided[role]).expanduser().resolve()
            if not cover.is_file():
                raise ValueError(f"The {role} publication cover is missing")
            info = media.probe(cover)
            codec = info.get("video_codec")
            if codec not in {"png", "mjpeg"}:
                raise ValueError("Publication covers must be PNG or JPEG images")
            if (info["width"], info["height"]) != (width, height) or abs(info.get("rotation", 0)) % 360:
                raise ValueError(f"The {role} publication cover must be exactly {width}x{height} ({ratio}) without rotation")
            if info.get("has_audio") or info.get("hdr"):
                raise ValueError("Publication covers must be SDR still images without audio")
            target = output / f"cover-{role}.{'png' if codec == 'png' else 'jpg'}"
            digest = _digest_file(cover)
            covers[role] = {"image": target.name, "width": width, "height": height,
                            "ratio": ratio, "sha256": digest, "codec": codec}
            copies.append((cover, target, digest))
    else:
        cover = Path(cover_source).expanduser().resolve() if cover_source else output / "cover.png"
        if not cover.is_file():
            raise ValueError("Cover PNG is missing; use the built-in image tool, then pass --cover")
        cover_info = media.probe(cover)
        if cover_info.get("video_codec") != "png":
            raise ValueError("The cover must be a PNG image")
        if abs(cover_info["width"] / cover_info["height"] - video["width"] / video["height"]) > 0.05:
            raise ValueError("Cover aspect ratio does not match the video")
        target = output / "cover.png"
        copies.append((cover, target, _digest_file(cover)))
    # Check every destination before copying either image. Invalid or conflicting
    # second covers must not partially replace an approved first cover.
    for cover, target, digest in copies:
        if target.resolve() == source:
            raise ValueError("Cover output must not replace the original source video")
        if cover.resolve() != target.resolve() and target.exists() and not overwrite and digest != _digest_file(target):
            raise FileExistsError("Cover exists; use --overwrite to replace it")
    if video["hdr"] or abs(video["rotation"]) % 360:
        raise ValueError("Output has unhandled HDR or rotation metadata")
    tolerance = 1 / max(video["fps"], 1) + 0.01
    if not video["has_audio"] or abs(video["video_duration"] - video["audio_duration"]) > tolerance:
        raise ValueError("Output audio/video durations do not align")
    cues = json.loads((output / "captions.json").read_text(encoding="utf-8"))
    if not cues or any(not 0 <= c["start"] < c["end"] <= video["duration"] + tolerance for c in cues):
        raise ValueError("Missing captions or captions outside the edited timeline")
    if any(a["end"] > b["start"] for a, b in zip(cues, cues[1:])):
        raise ValueError("Caption events overlap")
    media._run([media._binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-threads", "2",
                "-i", str(output / "edited.mp4"), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"])
    for cover, target, _ in copies:
        if cover.resolve() != target.resolve():
            shutil.copy2(cover, target)
    meta = {**old_meta, "title": title, "source_sha256": report["source_sha256"], "prompt": "cover-prompt.txt"}
    if publication:
        for key in ("image", "width", "height"):
            meta.pop(key, None)
        meta.update({"mode": "publication cover pair; visually reviewed by calling workflow", "covers": covers})
    else:
        meta.pop("covers", None)
        mode = old_meta.get("mode", "provided PNG") if cover_source is None else "provided PNG; generated by calling workflow"
        meta.update({"mode": mode, "image": "cover.png", "width": cover_info["width"], "height": cover_info["height"]})
    _write_json(output / "cover-meta.json", meta)
    verification = {"schema_version": 1, "source_sha256": report["source_sha256"],
                    "full_decode": "passed", "caption_timing": "passed", "title": title,
                    "video": video, "cover": meta, "removed_seconds": report["removed_seconds"],
                    "cover_lettering": "declared title; visual review is separate, not automatically OCR-verified"}
    _write_json(output / "verification.json", verification)
    prior = _read(output / "delivery.json") if (output / "delivery.json").exists() else {}
    cover_names = [item["image"] for item in covers.values()] if publication else ["cover.png"]
    job = {**prior, "schema_version": 1, "status": "complete", "source": str(source),
           "source_sha256": report["source_sha256"], "output_dir": str(output), "title": title,
           "edited_sha256": _digest_file(output / "edited.mp4"),
           "artifacts": [name for name in ("edited.mp4", "clean.mp4", *cover_names, "captions.srt", "verification.json")
                         if (output / name).is_file()]}
    if publication:
        job.pop("cover_sha256", None)
        job["covers"] = covers
    else:
        job.pop("covers", None)
        job["cover_sha256"] = _digest_file(output / "cover.png")
    _write_json(output / "delivery.json", job)
    return job


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare/render/finish one video for the TalkingEditor chat workflow")
    parser.add_argument("input", type=Path, help="One video; defaults to <stem>_output next to it")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--title", help="Render this content-selected title after preparing")
    parser.add_argument("--keyword", action="append", help="Optional subtitle emphasis; repeat for multiple terms")
    parser.add_argument("--cover-time", type=float, help="Prepare a different reference frame in source seconds")
    parser.add_argument("--finish", action="store_true", help="Validate the rendered video and generated cover")
    parser.add_argument("--cover", type=Path, help="PNG returned by the image tool")
    parser.add_argument("--cover-vertical", type=Path, help="1080x1440 (3:4) publication cover, PNG or JPEG")
    parser.add_argument("--cover-horizontal", type=Path, help="1440x1080 (4:3) publication cover, PNG or JPEG")
    parser.add_argument("--cover-title", help="Exact title visibly checked on the cover")
    parser.add_argument("--overwrite", action="store_true", help="Replace explicitly requested generated versions")
    args = parser.parse_args(argv)
    if any(value is not None for value in (args.cover, args.cover_title, args.cover_vertical, args.cover_horizontal)) and not args.finish:
        parser.error("Cover options require --finish")
    if (args.cover_vertical is None) != (args.cover_horizontal is None):
        parser.error("--cover-vertical and --cover-horizontal are required together")
    if args.cover is not None and args.cover_vertical is not None:
        parser.error("Use either --cover or the publication cover pair")
    if args.finish and args.title is not None:
        parser.error("Render with --title first, then validate using --finish")
    try:
        if args.finish:
            result = finish(args.input, args.output, cover_source=args.cover, cover_title=args.cover_title,
                            cover_vertical_source=args.cover_vertical, cover_horizontal_source=args.cover_horizontal,
                            overwrite=args.overwrite)
        elif args.title is not None:
            result = render(args.input, args.title, args.output, keywords=args.keyword, overwrite=args.overwrite)
        else:
            result = prepare(args.input, args.output, cover_time=args.cover_time)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
