"""Small, reusable FFmpeg adapter for the TalkingEditor edit timeline.

Video is selected in one decode pass and its timestamps are mapped onto the
edited timeline before a *single* frame-rate conversion. Audio is trimmed at
sample boundaries and concatenated separately. Consequently every cut is
quantized to at most one output video frame, without accumulating A/V drift.
No third-party Python packages are required by this module.
"""

from __future__ import annotations

import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any


class MediaError(RuntimeError):
    """An actionable failure to inspect or render media."""


_runtime_library_path: str | None = None
_runtime_compatibility_notes: list[str] = []


def _freetype_candidates(detail: str) -> list[Path]:
    candidates: list[Path] = []
    for cellar in (Path("/opt/homebrew/Cellar/freetype"), Path("/usr/local/Cellar/freetype")):
        if cellar.is_dir():
            candidates.extend(p.parent for p in cellar.glob("*/lib/libfreetype.6.dylib")
                              if str(p) not in detail)
    return sorted(candidates,
                  key=lambda p: tuple(int(n) for n in re.findall(r"\d+", p.parent.name)), reverse=True)


def _binary(name: str) -> str:
    override = os.environ.get(f"TALKING_EDITOR_{name.upper()}")
    if override:
        found = shutil.which(override)
        if found:
            return found
        raise MediaError(f"TALKING_EDITOR_{name.upper()} does not name an executable: {override}")
    # Homebrew's standard FFmpeg omits libass/zimg. Prefer the full keg without
    # requiring a global brew link or changing the user's PATH.
    for directory in ("/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/opt/ffmpeg-full/bin"):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    if name == "ffprobe":
        ffmpeg_override = os.environ.get("TALKING_EDITOR_FFMPEG")
        if ffmpeg_override:
            candidate = Path(ffmpeg_override).resolve().with_name("ffprobe")
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    found = shutil.which(name)
    if not found:
        raise MediaError(f"{name} was not found. Install FFmpeg and add it to PATH.")
    return found


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    global _runtime_library_path

    def execute(library_path: str | None = None) -> subprocess.CompletedProcess:
        environment = None
        if library_path:
            environment = dict(os.environ)
            inherited = environment.get("DYLD_LIBRARY_PATH", "")
            environment["DYLD_LIBRARY_PATH"] = library_path + (os.pathsep + inherited if inherited else "")
        return subprocess.run(
            command, cwd=cwd, check=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=environment,
        )

    try:
        completed = execute(_runtime_library_path)
    except OSError as exc:
        raise MediaError(f"Could not run {Path(command[0]).name}: {exc}") from exc
    detail = completed.stderr
    # A recent Homebrew FreeType bottle can require libhvf from a newer macOS.
    # Only this exact loader failure permits a compatibility retry. All library
    # changes are scoped to our subprocess; global links/PATH stay untouched.
    if (completed.returncode and "dyld" in detail and "libhvf.dylib" in detail
            and "freetype" in detail.lower()):
        for candidate in _freetype_candidates(detail):
            retried = execute(str(candidate))
            if retried.returncode == 0:
                _runtime_library_path = str(candidate)
                note = (f"FFmpeg uses retained FreeType {candidate.parent.name} through a subprocess-only "
                        "DYLD_LIBRARY_PATH because the current Homebrew FreeType requires unavailable libhvf.dylib.")
                if note not in _runtime_compatibility_notes:
                    _runtime_compatibility_notes.append(note)
                completed = retried
                break
        else:
            raise MediaError(
                "The installed FFmpeg depends on a FreeType build requiring libhvf.dylib from a newer macOS. "
                "No working retained FreeType keg was found. Install an FFmpeg/FreeType build compatible with this macOS.\n"
                + detail.strip()[-3000:]
            )
    if completed.returncode:
        detail = completed.stderr.strip()[-8000:] or "No diagnostic output."
        raise MediaError(f"{Path(command[0]).name} failed ({completed.returncode}):\n{detail}")
    return completed.stdout


def _ratio(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, str) and "/" in value:
            numerator, denominator = value.split("/", 1)
            return float(numerator) / float(denominator)
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def probe(path: Path | str) -> dict[str, Any]:
    """Inspect the first video/audio streams; dimensions include display rotation."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise MediaError(f"Input file does not exist: {source}")
    raw = json.loads(_run([
        _binary("ffprobe"), "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(source),
    ]))
    streams = raw.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaError(f"Input contains no video stream: {source}")
    rotation = _ratio(video.get("tags", {}).get("rotate"))
    for side_data in video.get("side_data_list", []):
        if "rotation" in side_data:
            rotation = _ratio(side_data["rotation"])
    width, height = int(video["width"]), int(video["height"])
    if round(rotation) % 180 == 90:
        width, height = height, width
    transfer = video.get("color_transfer", "unknown")
    return {
        "path": str(source),
        "duration": _ratio(video.get("duration"), _ratio(raw.get("format", {}).get("duration"))),
        "video_duration": _ratio(video.get("duration"), _ratio(raw.get("format", {}).get("duration"))),
        "audio_duration": _ratio(audio.get("duration")) if audio else 0.0,
        "width": width, "height": height,
        "coded_width": int(video["width"]), "coded_height": int(video["height"]),
        "fps": _ratio(video.get("avg_frame_rate"), _ratio(video.get("r_frame_rate"))),
        "nominal_fps": _ratio(video.get("r_frame_rate")),
        "hdr": transfer in {"smpte2084", "arib-std-b67"},
        "has_audio": audio is not None,
        "rotation": rotation, "color_transfer": transfer,
        "color_primaries": video.get("color_primaries", "unknown"),
        "color_space": video.get("color_space", "unknown"),
        "color_range": video.get("color_range", "unknown"),
        "video_codec": video.get("codec_name", "unknown"),
        "sample_rate": int(audio.get("sample_rate", 48000)) if audio else 0,
        "audio_channels": int(audio.get("channels", 0)) if audio else 0,
        "runtime_compatibility_notes": list(_runtime_compatibility_notes),
    }


def _paths(source: Path | str, destination: Path | str, overwrite: bool) -> tuple[Path, Path]:
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if not source.is_file():
        raise MediaError(f"Input file does not exist: {source}")
    if source == destination or (destination.exists() and os.path.samefile(source, destination)):
        raise MediaError("The output must differ from the input; original recordings are never overwritten.")
    if destination.exists() and not overwrite:
        raise MediaError(f"Output already exists: {destination}. Set overwrite=True to replace it.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return source, destination


def extract_audio(source: Path | str, destination: Path | str) -> None:
    """Extract 16 kHz mono PCM WAV for speech recognition, preserving timeline zero.

    An existing derived WAV may be replaced; the source itself is protected.
    The final path appears only after FFmpeg has completed successfully.
    """
    source, destination = _paths(source, destination, overwrite=True)
    if not probe(source)["has_audio"]:
        raise MediaError(f"Input contains no audio stream: {source}")
    with tempfile.TemporaryDirectory(prefix=".talking-audio-", dir=destination.parent) as tmp:
        temporary = Path(tmp) / "audio.wav"
        _run([
            _binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", "2", "-copyts", "-start_at_zero", "-i", str(source),
            "-map", "0:a:0", "-vn", "-af", "aresample=16000:first_pts=0",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(temporary),
        ])
        os.replace(temporary, destination)


@lru_cache(maxsize=8)
def _filters(ffmpeg: str) -> frozenset[str]:
    output = _run([ffmpeg, "-hide_banner", "-filters"])
    return frozenset(line.split()[1] for line in output.splitlines()
                     if len(line.split()) >= 3 and 2 <= len(line.split()[0]) <= 3
                     and set(line.split()[0]) <= {"T", "S", "."})


def _normalise_keep(keep: list[dict], duration: float) -> list[dict[str, float]]:
    if not keep:
        raise MediaError("The edit contains no retained intervals.")
    result: list[dict[str, float]] = []
    output_cursor = 0.0
    previous_end = 0.0
    for index, item in enumerate(keep):
        try:
            start, end = float(item["source_start"]), float(item["source_end"])
            output_start = float(item.get("output_start", output_cursor))
            output_end = float(item.get("output_end", output_start + end - start))
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaError(f"Invalid retained interval at index {index}.") from exc
        if not all(math.isfinite(n) for n in (start, end, output_start, output_end)):
            raise MediaError(f"Non-finite timestamp in retained interval {index}.")
        if start < 0 or end <= start or end > duration + 0.05:
            raise MediaError(f"Retained interval {index} lies outside the video: {start}–{end} seconds.")
        end = min(end, duration)
        if index and start < previous_end - 1e-6:
            raise MediaError("Retained intervals must be ordered and must not overlap.")
        if abs(output_start - output_cursor) > 0.001 or abs((output_end-output_start)-(end-start)) > 0.001:
            raise MediaError("Output intervals must form a contiguous timeline at original playback speed.")
        output_end = output_cursor + end - start
        result.append({"source_start": start, "source_end": end,
                       "output_start": output_cursor, "output_end": output_end})
        output_cursor, previous_end = output_end, end
    return result


def _number(value: float) -> str:
    return f"{value:.9f}".rstrip("0").rstrip(".") or "0"


def _video_timeline(keep: list[dict[str, float]], fps: float) -> str:
    selection = "+".join(
        f"gte(t,{_number(k['source_start'])})*lt(t,{_number(k['source_end'])})" for k in keep
    )
    offset = keep[0]["source_start"]
    mapping = _number(offset)
    for item in keep[1:]:
        next_offset = item["source_start"] - item["output_start"]
        gap = next_offset - offset
        if gap > 1e-9:
            mapping += f"+{_number(gap)}*gte(PTS*TB,{_number(item['source_start'])})"
        offset = next_offset
    # A single final conversion quantizes the cumulative timeline, rather than
    # adding a separately rounded duration for each small clip.
    return (f"settb=AVTB,trim=end={_number(keep[-1]['source_end'])},"
            f"select='{selection}',setpts='PTS-({mapping})/TB',"
            f"fps=fps={_number(fps)}:start_time=0:round=near:eof_action=pass,"
            f"tpad=stop_mode=clone:stop_duration=1,trim=duration={_number(keep[-1]['output_end'])}")


def _tonemap_desat(options: dict | None = None) -> float:
    """Read the HDR highlight desaturation setting without accepting NaN/inf."""
    raw = (options or {}).get("tonemap_desat", 2)
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaError("tonemap_desat must be a finite nonnegative number.") from exc
    if isinstance(raw, bool) or not math.isfinite(value) or value < 0:
        raise MediaError("tonemap_desat must be a finite nonnegative number.")
    return value


def _colour_filters(metadata: dict, available: frozenset[str], options: dict | None = None) -> list[str]:
    desat = _tonemap_desat(options)
    if metadata["hdr"]:
        required = {"zscale", "tonemap"}
        if not required <= available:
            raise MediaError(
                "HDR input needs FFmpeg with zscale and tonemap filters. "
                "On macOS install brew install ffmpeg-full, or set TALKING_EDITOR_FFMPEG "
                "to a full build. Relabelling HDR as SDR would produce incorrect colours."
            )
        transfer = metadata["color_transfer"]
        primaries = metadata["color_primaries"]
        matrix = metadata["color_space"]
        if primaries == "unknown":
            primaries = "bt2020"
        if matrix == "unknown":
            matrix = "bt2020nc"
        input_range = "full" if metadata["color_range"] == "pc" else "limited"
        # Convert HLG/PQ to floating-point linear light at 100-nit reference,
        # reduce BT.2020 gamut to BT.709, then map a nominal 1000-nit peak to SDR.
        return [
            f"zscale=transferin={transfer}:primariesin={primaries}:matrixin={matrix}:"
            f"rangein={input_range}:transfer=linear:npl=100",
            "format=gbrpf32le", "zscale=primaries=bt709",
            f"tonemap=tonemap=hable:desat={desat:.15g}:peak=10",
            "zscale=transfer=bt709:matrix=bt709:range=limited", "format=yuv420p",
        ]
    # Most SDR camera files are BT.709; explicitly convert tagged wide-gamut
    # SDR when zimg is available rather than merely changing its colour tags.
    if metadata["color_primaries"] not in {"unknown", "bt709"}:
        if "zscale" not in available:
            raise MediaError("Wide-gamut video conversion needs the zscale filter in a full FFmpeg build.")
        return ["zscale=primaries=bt709:transfer=bt709:matrix=bt709:range=limited", "format=yuv420p"]
    return ["scale=out_color_matrix=bt709:out_range=tv", "format=yuv420p"]


def _audio_graph(keep: list[dict[str, float]], fade_ms: float) -> list[str]:
    sample_rate = 48000
    branches = "".join(f"[a{i}]" for i in range(len(keep)))
    graph = [f"[0:a:0]aresample={sample_rate}:first_pts=0,asetpts=N/SR/TB,asplit={len(keep)}{branches}"]
    for index, item in enumerate(keep):
        source_start = round(item["source_start"] * sample_rate)
        # Cumulative output rounding bounds total error to one audio sample.
        length = round(item["output_end"]*sample_rate) - round(item["output_start"]*sample_rate)
        fade = min(round(fade_ms*sample_rate/1000), length//4)
        filters = [f"atrim=start_sample={source_start}:end_sample={source_start+length}", "asetpts=N/SR/TB"]
        if fade:
            filters += [f"afade=t=in:ss=0:ns={fade}", f"afade=t=out:ss={length-fade}:ns={fade}"]
        graph.append(f"[a{index}]" + ",".join(filters) + f"[cut{index}]")
    graph.append("".join(f"[cut{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=0:a=1[aout]")
    return graph


def render(
    source: Path | str, destination: Path | str, keep: list[dict],
    ass_path: Path | str | None = None, options: dict | None = None,
) -> dict[str, Any]:
    """Render a contiguous edit as physically oriented SDR H.264/AAC MP4.

    Options: overwrite=False, crf=20, preset='medium', threads=4, fps=source
    nominal rate, audio_fade_ms=4, max_width/max_height=None. Optional dimensions
    bound the result without upscaling. tonemap_desat=2 controls HDR highlight
    desaturation; use 0 to preserve highlight chroma after reviewing the source.
    Hard video cuts are retained; short
    audio edge fades suppress clicks without changing timeline duration.
    """
    options = dict(options or {})
    source, destination = _paths(source, destination, bool(options.get("overwrite", False)))
    metadata = probe(source)
    intervals = _normalise_keep(keep, metadata["duration"])
    ffmpeg = _binary("ffmpeg")
    available = _filters(ffmpeg)
    colour = _colour_filters(metadata, available, options)
    fps = _ratio(options.get("fps"), metadata["nominal_fps"] or metadata["fps"] or 30)
    if not math.isfinite(fps) or not 1 <= fps <= 240:
        raise MediaError("Output fps must be between 1 and 240.")
    threads = max(1, min(int(options.get("threads", 4)), 8))
    fade_ms = float(options.get("audio_fade_ms", 4))
    if not math.isfinite(fade_ms) or not 0 <= fade_ms <= 20:
        raise MediaError("audio_fade_ms must be between 0 and 20.")
    crf = int(options.get("crf", 20))
    if not 0 <= crf <= 51:
        raise MediaError("crf must be between 0 and 51.")
    preset = str(options.get("preset", "medium"))
    if preset not in {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}:
        raise MediaError(f"Unsupported x264 preset: {preset}")
    width, height = metadata["width"], metadata["height"]
    factor = 1.0
    for key, dimension in (("max_width", width), ("max_height", height)):
        if options.get(key) is not None:
            maximum = int(options[key])
            if maximum < 2:
                raise MediaError(f"{key} must be at least 2 pixels.")
            factor = min(factor, maximum / dimension)
    width, height = max(2, int(width*factor)//2*2), max(2, int(height*factor)//2*2)
    captions = Path(ass_path).expanduser().resolve() if ass_path is not None else None
    if captions and not captions.is_file():
        raise MediaError(f"Subtitle file does not exist: {captions}")
    if captions and "ass" not in available:
        raise MediaError("Burning subtitles needs FFmpeg's ass filter (libass). Install a full FFmpeg build.")
    duration = intervals[-1]["output_end"]
    with tempfile.TemporaryDirectory(prefix=".talking-render-", dir=destination.parent) as tmp:
        temporary_directory = Path(tmp)
        temporary = temporary_directory / "output.mp4"
        video_filters = [_video_timeline(intervals, fps)]
        if (width, height) != (metadata["width"], metadata["height"]):
            video_filters.append(f"scale={width}:{height}:flags=lanczos")
        video_filters += colour
        if captions:
            # A safe local name avoids FFmpeg filter escaping issues with
            # spaces, Chinese paths, apostrophes, commas and drive colons.
            shutil.copyfile(captions, temporary_directory / "captions.ass")
            video_filters.append("ass=filename=captions.ass")
        video_filters += ["setsar=1", "sidedata=mode=delete",
                          "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709"]
        graph = ["[0:v:0]" + ",".join(video_filters) + "[vout]"]
        if metadata["has_audio"]:
            graph += _audio_graph(intervals, fade_ms)
        (temporary_directory / "edit.ffscript").write_text(";\n".join(graph), encoding="utf-8")
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", str(threads), "-filter_complex_threads", str(threads),
            "-copyts", "-start_at_zero", "-i", str(source),
            "-/filter_complex", "edit.ffscript", "-map", "[vout]",
        ]
        if metadata["has_audio"]:
            command += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        command += [
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-threads", str(threads),
            "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-t", _number(duration),
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-color_range", "tv", "-map_metadata", "-1", "-map_chapters", "-1",
            "-metadata:s:v:0", "rotate=0", "-movflags", "+faststart", str(temporary),
        ]
        _run(command, cwd=temporary_directory)
        rendered = probe(temporary)
        if rendered["width"] != width or rendered["height"] != height:
            raise MediaError("Rendered dimensions do not match the input display orientation.")
        if rendered["hdr"] or round(rendered["rotation"]) % 360:
            raise MediaError("Output still carries HDR or rotation metadata; refusing to publish it.")
        os.replace(temporary, destination)
    rendered["path"] = str(destination)
    rendered["requested_duration"] = duration
    rendered["actual_video_duration"] = rendered["video_duration"]
    rendered["tone_mapped"] = metadata["hdr"]
    rendered["colour_filters"] = colour
    rendered["limitations"] = [
        f"Cut positions are quantized once to the {fps:g} fps output grid (within one frame).",
        "Video uses hard cuts; audio uses sample-accurate cuts with short edge fades.",
    ]
    if metadata["hdr"]:
        rendered["limitations"].append("HDR is converted to SDR BT.709 with a nominal 1000-nit peak; Dolby Vision dynamic metadata is not applied.")
    return rendered


def burn_subtitles(
    source: Path | str, destination: Path | str, ass_path: Path | str,
    options: dict | None = None,
) -> dict[str, Any]:
    """Create a captioned version of an existing edit using the same renderer."""
    duration = probe(source)["duration"]
    return render(source, destination, [{"source_start": 0, "source_end": duration,
                                        "output_start": 0, "output_end": duration}], ass_path, options)
