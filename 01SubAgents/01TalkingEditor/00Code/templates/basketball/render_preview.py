#!/usr/bin/env python3
"""Render reviewed basketball captions beside the action, preserving the full timeline."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_media():
    # The template works directly from the shared engine or after installation.
    src = Path(__file__).resolve().parents[2] / "src"
    if (src / "talking_editor/media.py").is_file():
        sys.path.insert(0, str(src))
    try:
        from talking_editor import media
    except ImportError as exc:
        raise ValueError("Install TalkingEditor in the Python environment used for this template.") from exc
    return media


def write_captions(plan: dict, metadata: dict, directory: Path) -> list[dict]:
    # load_media() has already located the installed or adjacent engine.
    from talking_editor.basketball_captions import write_captions as write_basketball_captions
    return write_basketball_captions(plan, metadata, directory)


def timeline(media, path: Path, kind: str) -> dict:
    selector = "v:0" if kind == "video" else "a:0"
    command = [media._binary("ffprobe"), "-v", "error", "-select_streams", selector]
    if kind == "video":
        command += ["-show_frames", "-show_streams", "-show_entries",
                    "stream=time_base:frame=best_effort_timestamp,pts"]
        key, timestamp = "frames", "best_effort_timestamp"
    else:
        command += ["-show_packets", "-show_streams", "-show_data_hash", "sha256",
                    "-show_entries", "stream=time_base:packet=pts,duration,data_hash,size"]
        key, timestamp = "packets", "pts"
    data = json.loads(media._run(command + ["-of", "json", str(path)]))
    if len(data.get("streams", [])) != 1 or not data.get(key):
        raise ValueError(f"Cannot inspect the complete {kind} timeline: {path}")
    time_base = Fraction(data["streams"][0]["time_base"])
    items = data[key]
    times = []
    for item in items:
        value = item.get(timestamp, item.get("pts"))
        if value is None:
            raise ValueError(f"Missing {kind} presentation timestamp.")
        times.append(int(value) * time_base)
    return {"items": items, "times": times, "time_base": time_base}


def differences(source: list[Fraction], output: list[Fraction], tolerance: Fraction) -> dict:
    same_count = len(source) == len(output)
    source_deltas = [b - a for a, b in zip(source, source[1:])]
    output_deltas = [b - a for a, b in zip(output, output[1:])]
    maximum = max((abs(a - b) for a, b in zip(source_deltas, output_deltas)), default=Fraction(0))
    relative = max((abs((a - source[0]) - (b - output[0]))
                    for a, b in zip(source, output)), default=Fraction(0))
    return {"source_count": len(source), "output_count": len(output), "same_count": same_count,
            "source_first_pts_seconds": float(source[0]), "output_first_pts_seconds": float(output[0]),
            "source_last_pts_seconds": float(source[-1]), "output_last_pts_seconds": float(output[-1]),
            "source_strictly_increasing": all(d > 0 for d in source_deltas),
            "output_strictly_increasing": all(d > 0 for d in output_deltas),
            "max_interval_difference_seconds": float(maximum),
            "max_relative_pts_difference_seconds": float(relative),
            "timebase_rounding_tolerance_seconds": float(tolerance),
            "intervals_preserved": same_count and maximum <= tolerance,
            "relative_pts_preserved": same_count and relative <= tolerance,
            "source_intervals_sha256": hashlib.sha256("\n".join(map(str, source_deltas)).encode()).hexdigest(),
            "output_intervals_sha256": hashlib.sha256("\n".join(map(str, output_deltas)).encode()).hexdigest()}


def verify(media, source: Path, output: Path, source_hash: str, display_output: Path) -> dict:
    original, result = media.probe(source), media.probe(output)
    sv, ov = timeline(media, source, "video"), timeline(media, output, "video")
    sa, oa = timeline(media, source, "audio"), timeline(media, output, "audio")
    video_tolerance = max(Fraction(1, 1000), sv["time_base"] / 2, ov["time_base"] / 2)
    audio_tolerance = max(Fraction(1, 1000), sa["time_base"] / 2, oa["time_base"] / 2)
    video_comparison = differences(sv["times"], ov["times"], video_tolerance)
    audio_comparison = differences(sa["times"], oa["times"], audio_tolerance)
    source_hashes = [p.get("data_hash") for p in sa["items"]]
    output_hashes = [p.get("data_hash") for p in oa["items"]]
    if not all(isinstance(h, str) and h for h in source_hashes + output_hashes):
        raise ValueError("ffprobe did not provide SHA-256 for every encoded audio packet.")
    hashes_preserved = (all(source_hashes) and source_hashes == output_hashes)
    audio_comparison["payload_hashes_identical_in_order"] = bool(hashes_preserved)
    audio_comparison["source_hash_sequence_sha256"] = hashlib.sha256("\n".join(source_hashes).encode()).hexdigest()
    audio_comparison["output_hash_sequence_sha256"] = hashlib.sha256("\n".join(output_hashes).encode()).hexdigest()
    av_difference = abs((sa["times"][0] - sv["times"][0]) - (oa["times"][0] - ov["times"][0]))
    decode = {}
    for label, path in (("source", source), ("output", output)):
        try:
            media._run([media._binary("ffmpeg"), "-hide_banner", "-v", "error", "-xerror", "-nostdin",
                        "-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"])
            decode[label] = {"passed": True}
        except Exception as exc:
            decode[label] = {"passed": False, "error": str(exc)}
    video_duration_difference = abs(original["video_duration"] - result["video_duration"])
    audio_duration_difference = abs(original["audio_duration"] - result["audio_duration"])
    checks = {
        "source_fingerprint_unchanged": sha256(source) == source_hash,
        "decoded_video_frame_count_preserved": video_comparison["same_count"],
        "every_video_pts_interval_preserved": video_comparison["intervals_preserved"],
        "entire_video_relative_pts_preserved": video_comparison["relative_pts_preserved"],
        "video_frame_order_preserved": video_comparison["source_strictly_increasing"] and video_comparison["output_strictly_increasing"],
        "audio_packet_count_preserved": audio_comparison["same_count"],
        "audio_packet_payload_hashes_preserved": bool(hashes_preserved),
        "audio_packet_pts_intervals_preserved": audio_comparison["intervals_preserved"],
        "entire_audio_relative_pts_preserved": audio_comparison["relative_pts_preserved"],
        "audio_packet_order_preserved": audio_comparison["source_strictly_increasing"] and audio_comparison["output_strictly_increasing"],
        "av_start_offset_preserved": av_difference <= max(video_tolerance, audio_tolerance),
        "video_duration_preserved": video_duration_difference <= 0.001,
        "audio_duration_preserved": audio_duration_difference <= 1 / original["sample_rate"] + 0.000001,
        "display_dimensions_preserved": (original["width"], original["height"]) == (result["width"], result["height"]),
        "audio_sample_rate_and_channels_preserved": (original["sample_rate"], original["audio_channels"]) == (result["sample_rate"], result["audio_channels"]),
        "sdr_bt709_output": not result["hdr"] and all(result[k] == "bt709" for k in ("color_transfer", "color_primaries", "color_space")),
        "physical_display_orientation": round(result["rotation"]) % 360 == 0,
        "complete_source_decode": decode["source"]["passed"],
        "complete_output_decode": decode["output"]["passed"],
    }
    result["path"] = str(display_output)
    return {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "passed" if all(checks.values()) else "failed", "checks": checks,
            "source": str(source), "source_sha256": source_hash, "output": str(display_output),
            "output_sha256": sha256(output), "source_metadata": original, "output_metadata": result,
            "video_frame_pts_comparison": video_comparison, "audio_packet_comparison": audio_comparison,
            "av_start_offset_difference_seconds": float(av_difference),
            "video_duration_difference_seconds": video_duration_difference,
            "audio_duration_difference_seconds": audio_duration_difference, "full_decode": decode,
            "colour_observation": {
                "source_is_hdr": original["hdr"],
                "source_tags": {k: original[k] for k in ("color_transfer", "color_primaries", "color_space")},
                "output_tags": {k: result[k] for k in ("color_transfer", "color_primaries", "color_space")},
                "output_is_sdr_bt709": checks["sdr_bt709_output"],
                "note": "Actual conversion filters are recorded in render-command.json; output tags alone do not prove correct tone mapping."
            },
            "method": "Decoded video frames and every presentation interval; encoded audio packet SHA-256 and PTS in order; complete audio/video decode. No visual similarity claim is made."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Your own source video, kept unchanged")
    parser.add_argument("--output", type=Path, required=True, help="Separate directory for preview and reports")
    parser.add_argument("--plan", type=Path, required=True, help="Reviewed caption-plan.json")
    parser.add_argument("--overwrite", action="store_true", help="Replace derived preview/subtitle/report files only")
    parser.add_argument("--verify", action="store_true", help="Verify every frame timestamp, copied audio packet and complete decode before accepting the render")
    parser.add_argument("--verify-only", action="store_true", help="Verify the existing preview without rendering")
    parser.add_argument("--tonemap-desat", type=float, default=2,
                        help="HDR highlight desaturation; preview your own material before adjusting")
    args = parser.parse_args()
    media = load_media()
    source = args.input.expanduser().resolve()
    if not source.is_file():
        raise ValueError("Input video does not exist.")
    source_hash = sha256(source)
    metadata = media.probe(source)
    if not metadata["has_audio"]:
        raise ValueError("This task requires the recorded original audio track.")
    if not metadata["hdr"] and any(metadata[k] != "bt709" for k in ("color_transfer", "color_primaries", "color_space")):
        raise ValueError("Unsupported SDR colour tags; this task accepts BT.709 SDR or explicitly mapped HDR.")
    if not math.isfinite(args.tonemap_desat) or args.tonemap_desat < 0:
        raise ValueError("tonemap-desat must be finite and nonnegative.")
    output_dir = args.output.expanduser().resolve()
    destination = output_dir / "preview.mp4"
    output_dir.mkdir(parents=True, exist_ok=True)
    derived_names = ("preview.mp4", "captions.ass", "captions.srt", "render-command.json", "verification.json")
    if any((output_dir / name).resolve() == source for name in derived_names):
        raise ValueError("Output must never overwrite the original.")
    if args.verify_only:
        report = verify(media, source, destination, source_hash, destination)
        write_json(output_dir / "verification.json", report)
        print(json.dumps({"verification": report["status"], "report": str(output_dir / "verification.json")}, ensure_ascii=False))
        return 0 if report["status"] == "passed" else 2
    derived = [output_dir / name for name in ("preview.mp4", "captions.ass", "captions.srt", "render-command.json")]
    if not args.overwrite and any(p.exists() for p in derived):
        raise ValueError("A derived output already exists; pass --overwrite to replace it.")
    plan_path = args.plan.expanduser().resolve()
    if any((output_dir / name).resolve() == plan_path for name in derived_names):
        raise ValueError("Output must not overwrite the caption plan.")
    plan = read_json(plan_path)
    ffmpeg = media._binary("ffmpeg")
    colour_filters = (media._colour_filters(metadata, media._filters(ffmpeg),
                                          {"tonemap_desat": args.tonemap_desat})
                      if metadata["hdr"] else ["format=yuv420p"])
    colour_record = {
        "mode": "HDR_to_SDR_BT709" if metadata["hdr"] else "SDR_BT709_unchanged",
        "source_tags": {k: metadata[k] for k in ("color_transfer", "color_primaries", "color_space", "color_range")},
        "filters": colour_filters,
        "tonemap_desat": args.tonemap_desat if metadata["hdr"] else None,
        "shared_adapter": "talking_editor.media._colour_filters",
        "additional_exposure_or_white_balance_adjustments": False,
    }
    with tempfile.TemporaryDirectory(prefix=".caption-preview-", dir=output_dir) as scratch:
        temporary_dir = Path(scratch)
        cues = write_captions(plan, metadata, temporary_dir)
        temporary_video = temporary_dir / "preview.mp4"
        filters = ",".join(colour_filters + [
            "ass=filename=captions.ass", "setsar=1", "sidedata=mode=delete",
            "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709"])
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-copyts", "-start_at_zero", "-i", str(source),
                   "-map", "0:v:0", "-map", "0:a:0", "-vf", filters,
                   "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-threads", "4",
                   "-fps_mode:v", "passthrough", "-enc_time_base:v", "demux",
                   "-c:a", "copy", "-pix_fmt", "yuv420p",
                   "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
                   "-avoid_negative_ts", "disabled", "-map_metadata", "-1", "-map_chapters", "-1",
                   "-metadata:s:v:0", "rotate=0", "-movflags", "+faststart", str(temporary_video)]
        media._run(command, cwd=temporary_dir)
        report = verify(media, source, temporary_video, source_hash, destination) if args.verify else None
        if report is not None and report["status"] != "passed":
            write_json(output_dir / "verification.json", report)
            raise ValueError("Verification failed; the temporary render was not promoted to preview.mp4. See verification.json.")
        for name in ("captions.ass", "captions.srt"):
            os.replace(temporary_dir / name, output_dir / name)
        os.replace(temporary_video, destination)
        record = {"schema_version": 1, "rendered_at": datetime.now(timezone.utc).isoformat(),
                  "source": str(source), "source_sha256": source_hash, "caption_plan": str(plan_path),
                  "caption_plan_sha256": sha256(plan_path), "caption_count": len(cues),
                  "caption_placement": {
                      "mode": plan.get("placement_mode", "near_action"),
                      "cues": [{"id": c.get("id"), "start": c["start"], "end": c["end"],
                                "text": c["text"], "layout": c["layout"]} for c in cues],
                      "automatic_tracking": False,
                      "review_required": "Inspect each cue's actual start, middle and end frames for people, face, ball, basket and play trajectory overlap"},
                  "output": str(destination), "output_sha256": sha256(destination),
                  "argv": command, "working_directory": str(temporary_dir),
                  "filter": filters, "audio": "stream copy; no resampling, normalization, cuts or fades",
                  "video_timeline": "passthrough at demuxer time base; no trim, fps filter, padding, speed change or persistent title",
                  "color": ("Source HDR is converted in linear light from its tagged HLG/BT2020 to SDR BT709 with hable tone mapping; no additional exposure, white-balance or saturation adjustment"
                            if metadata["hdr"] else "Source SDR BT709; no exposure, white-balance, saturation or tone-map adjustment"),
                  "colour_conversion": colour_record,
                  "runtime_compatibility_notes": list(media._runtime_compatibility_notes),
                  "verification_status": report["status"] if report else "not_requested"}
        write_json(output_dir / "render-command.json", record)
        if report:
            write_json(output_dir / "verification.json", report)
    print(json.dumps({"preview": str(destination), "captions": len(cues),
                      "verification": report["status"] if report else "not_requested"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        print(f"Preview error: {exc}", file=sys.stderr)
        raise SystemExit(1)
