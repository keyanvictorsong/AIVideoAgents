"""Local speech recognition with independent voice activity timestamps.

The input is a mono, signed PCM16, 16 kHz WAV on the original media timeline.
Whisper is deliberately run without VAD filtering: recognition is evidence for
captions, while ``speech_regions`` is separate evidence for the edit planner.
Heavy dependencies are imported only when :func:`transcribe` is called.

Backends: https://github.com/ml-explore/mlx-examples/tree/main/whisper
          https://github.com/SYSTRAN/faster-whisper
"""

from __future__ import annotations

import importlib
import importlib.util
import math
import os
from pathlib import Path
import platform
from typing import Any, Callable
import wave


SCHEMA_VERSION = 1
SAMPLE_RATE = 16_000
DEFAULT_MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_FASTER_MODEL = "small"

# Low onset threshold and padding favor preserving quiet syllables. The editor
# separately decides how much of a pause to remove and how many cuts to allow.
VAD_CONFIG = {
    "threshold": 0.35,
    "min_speech_duration_ms": 80,
    "min_silence_duration_ms": 360,
    "speech_pad_ms": 120,
}
INITIAL_PROMPT_ZH = (
    "普通话口语，简体中文。逐字保留实际说出的口头语、重复和语气词，"
    "例如：嗯，呃，啊，那个，就是，然后。"
)

Progress = Callable[[str], None]


class SpeechError(RuntimeError):
    """Recognition or VAD failed; no synthetic transcript is returned."""


def _notify(progress: Progress | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _dependency(name: str, install_name: str) -> Any:
    try:
        return importlib.import_module(name)
    except (ImportError, OSError) as exc:
        raise SpeechError(
            f"Cannot load {name}: {exc}. Install {install_name} in the same "
            "Python environment used to run TalkingEditor."
        ) from exc


def _read_audio(path: Path) -> tuple[Any, float]:
    numpy = _dependency("numpy", "faster-whisper==1.2.1")
    try:
        with wave.open(str(path), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != SAMPLE_RATE
                or source.getcomptype() != "NONE"
            ):
                raise ValueError(
                    "Expected mono PCM16 WAV at 16000 Hz; extract audio with "
                    "ffmpeg -i INPUT -vn -ac 1 -ar 16000 -c:a pcm_s16le AUDIO.wav"
                )
            frames = source.getnframes()
            raw = source.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"Invalid WAV audio: {path}: {exc}") from exc
    if not frames or len(raw) != frames * 2:
        raise ValueError(f"Audio is empty or truncated: {path}")
    # WAV stores signed 16-bit samples in little endian, including on ARM Macs.
    audio = numpy.frombuffer(raw, dtype="<i2").astype(numpy.float32) / 32768.0
    return audio, frames / SAMPLE_RATE


def _speech_regions(audio: Any, duration: float) -> list[dict[str, float]]:
    vad = _dependency("faster_whisper.vad", "faster-whisper==1.2.1")
    try:
        timestamps = vad.get_speech_timestamps(
            audio,
            vad_options=vad.VadOptions(**VAD_CONFIG),
            sampling_rate=SAMPLE_RATE,
        )
    except Exception as exc:
        raise SpeechError(
            "Independent Silero voice detection failed. Ensure faster-whisper "
            f"and onnxruntime are installed correctly: {exc}"
        ) from exc
    regions = []
    for region in timestamps:
        start = max(0.0, min(duration, float(region["start"]) / SAMPLE_RATE))
        end = max(0.0, min(duration, float(region["end"]) / SAMPLE_RATE))
        if end > start:
            regions.append({"start": round(start, 6), "end": round(end, 6)})
    return regions


def _cached_mlx_snapshot(model: str) -> Path | None:
    """Resolve cached weights locally, avoiding a Hub network request on reruns."""
    hf_home = Path(os.environ.get("HF_HOME", "~/.cache/huggingface")).expanduser()
    hub = Path(os.environ.get("HF_HUB_CACHE", str(hf_home / "hub"))).expanduser()
    model_cache = hub / ("models--" + model.replace("/", "--"))
    snapshots = model_cache / "snapshots"
    candidates: list[Path] = []
    reference = model_cache / "refs" / "main"
    if reference.is_file():
        candidates.append(snapshots / reference.read_text().strip())
    if snapshots.is_dir():
        candidates.extend(sorted(snapshots.iterdir(), reverse=True))
    for candidate in candidates:
        if (candidate / "config.json").is_file() and any(
            (candidate / filename).is_file()
            for filename in ("weights.safetensors", "weights.npz")
        ):
            return candidate
    return None


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _resolve_backend(backend: str, model: str | None) -> tuple[str, str]:
    aliases = {
        "auto": "auto",
        "mlx": "mlx",
        "mlx-whisper": "mlx",
        "mlx_whisper": "mlx",
        "faster": "faster-whisper",
        "faster-whisper": "faster-whisper",
        "faster_whisper": "faster-whisper",
    }
    selected = aliases.get(backend.lower())
    if selected is None:
        raise ValueError("backend must be auto, mlx, or faster-whisper")
    if selected == "auto":
        apple_silicon = platform.system() == "Darwin" and platform.machine() == "arm64"
        local_model = Path(model).expanduser() if model else None
        mlx_model = bool(model and model.startswith("mlx-community/")) or bool(
            local_model and (local_model / "weights.safetensors").is_file()
        )
        if mlx_model:
            selected = "mlx"
        elif model:
            selected = "faster-whisper"
        elif apple_silicon and _has_module("mlx_whisper") and _cached_mlx_snapshot(DEFAULT_MLX_MODEL):
            selected = "mlx"
        else:
            selected = "faster-whisper"
    return selected, model or (DEFAULT_MLX_MODEL if selected == "mlx" else DEFAULT_FASTER_MODEL)


def _transcribe_mlx(audio: Any, model: str, language: str | None) -> tuple[list[Any], str]:
    mlx_whisper = _dependency("mlx_whisper", "mlx-whisper==0.4.3")
    cached = _cached_mlx_snapshot(model) if "/" in model and not Path(model).exists() else None
    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=str(cached) if cached else model,
        language=language,
        task="transcribe",
        word_timestamps=True,
        initial_prompt=INITIAL_PROMPT_ZH if language == "zh" else None,
        condition_on_previous_text=False,
        temperature=0.0,
        verbose=None,
    )
    return list(result.get("segments", [])), result.get("language") or language or "und"


def _transcribe_faster(
    audio: Any, model: str, language: str | None, progress: Progress | None
) -> tuple[list[Any], str]:
    faster_whisper = _dependency("faster_whisper", "faster-whisper==1.2.1")
    recognizer = faster_whisper.WhisperModel(
        model,
        device="cpu",
        compute_type="int8",
        cpu_threads=min(os.cpu_count() or 4, 8),
    )
    segments, info = recognizer.transcribe(
        audio,
        language=language,
        task="transcribe",
        word_timestamps=True,
        vad_filter=False,
        initial_prompt=INITIAL_PROMPT_ZH if language == "zh" else None,
        condition_on_previous_text=False,
        temperature=0.0,
        beam_size=5,
    )
    result = []
    # faster-whisper is lazy: errors can arise while consuming the generator.
    for segment in segments:
        result.append(segment)
        _notify(progress, f"Recognized speech through {segment.end:.1f}s")
    return result, info.language or language or "und"


def _field(record: Any, key: str, default: Any = None) -> Any:
    return record.get(key, default) if isinstance(record, dict) else getattr(record, key, default)


def _time_range(record: Any, duration: float) -> tuple[float, float] | None:
    try:
        start, end = float(_field(record, "start")), float(_field(record, "end"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(start) or not math.isfinite(end) or end < start:
        return None
    if start > duration or end < 0:
        return None
    return round(max(0.0, start), 6), round(min(duration, end), 6)


def _normalise_segments(
    raw_segments: list[Any], duration: float, warnings: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    words: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    invalid_words = zero_length_words = missing_alignment = 0
    for segment in raw_segments:
        times = _time_range(segment, duration)
        text = str(_field(segment, "text", "")).strip()
        if times is not None and text:
            segments.append({"start": times[0], "end": times[1], "text": text})
        raw_words = _field(segment, "words", []) or []
        if text and not raw_words:
            missing_alignment += 1
        for raw_word in raw_words:
            word_text = str(_field(raw_word, "word", _field(raw_word, "text", ""))).strip()
            if not word_text:
                continue
            word_times = _time_range(raw_word, duration)
            if word_times is None:
                invalid_words += 1
                continue
            probability = _field(raw_word, "probability")
            try:
                probability = float(probability)
                if not math.isfinite(probability) or not 0 <= probability <= 1:
                    probability = None
            except (TypeError, ValueError):
                probability = None
            if word_times[0] == word_times[1]:
                zero_length_words += 1
            words.append(
                {
                    "id": len(words),
                    "text": word_text,
                    "start": word_times[0],
                    "end": word_times[1],
                    "probability": probability,
                }
            )
    if missing_alignment:
        raise SpeechError(
            f"{missing_alignment} recognized segment(s) have no word timestamps. "
            "Cannot safely align captions or remove filler audio; no timestamps were invented."
        )
    if invalid_words:
        raise SpeechError(
            f"Recognition returned {invalid_words} word(s) with invalid timestamps. "
            "Cannot safely create an edit plan; retry with another speech model."
        )
    if zero_length_words:
        warnings.append(
            f"{zero_length_words} word(s) have zero-duration model alignment; "
            "keep their audio and review nearby edits."
        )
    if any(a["start"] > b["start"] for a, b in zip(words, words[1:])):
        warnings.append("Word timestamps are not monotonic; review edits near overlapping speech.")
    return words, segments


def transcribe(
    audio_path: str | Path,
    backend: str = "auto",
    model: str | None = None,
    language: str | None = "zh",
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Recognize the original audio and detect speech independently.

    ``progress``, when supplied, receives human-readable status strings. ``auto``
    uses a cached MLX large-v3-turbo on Apple Silicon, otherwise CPU int8
    faster-whisper. Passing a model explicitly avoids changing that model after
    a backend failure. A model name not already cached may download its weights.

    ``language=None`` or ``"auto"`` enables language detection. The default
    Chinese prompt asks for verbatim Simplified Chinese including fillers; the
    model can still miss fillers, so its transcript is not a perfect alignment.
    Exceptions propagate as ``SpeechError`` instead of returning fake text.
    All reported times are seconds in the original WAV timeline.
    """
    path = Path(audio_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Audio does not exist: {path}")
    selected, selected_model = _resolve_backend(backend, model)
    language = None if language in (None, "auto") else language
    if language and language.lower() in ("zh-cn", "zh-tw", "chinese"):
        language = "zh"
    _notify(progress, "Detecting speech independently with Silero VAD")
    audio, duration = _read_audio(path)
    regions = _speech_regions(audio, duration)
    warnings: list[str] = []
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "language": language or "und",
        "duration": duration,
        "backend": selected,
        "model": selected_model,
        "words": [],
        "segments": [],
        "speech_regions": regions,
        "warnings": warnings,
    }
    if not regions:
        warnings.append("Silero VAD detected no speech. No transcript was generated; review the input.")
        return result
    _notify(progress, f"Transcribing with {selected}: {selected_model}")
    try:
        if selected == "mlx":
            try:
                raw_segments, detected_language = _transcribe_mlx(audio, selected_model, language)
            except Exception as exc:
                if backend.lower() != "auto" or model is not None:
                    raise
                warnings.append(f"MLX recognition failed ({type(exc).__name__}: {exc}); used CPU fallback.")
                selected, selected_model = "faster-whisper", DEFAULT_FASTER_MODEL
                _notify(progress, f"Using CPU fallback: {selected_model}")
                raw_segments, detected_language = _transcribe_faster(audio, selected_model, language, progress)
        else:
            raw_segments, detected_language = _transcribe_faster(audio, selected_model, language, progress)
        words, segments = _normalise_segments(raw_segments, duration, warnings)
    except SpeechError:
        raise
    except Exception as exc:
        raise SpeechError(f"Speech recognition failed with {selected} ({selected_model}): {exc}") from exc
    if not words:
        raise SpeechError(
            "Speech activity was detected but the model returned no timed words. "
            "Try another model or language; no empty transcript was substituted."
        )
    result.update(
        language=detected_language,
        backend=selected,
        model=selected_model,
        words=words,
        segments=segments,
    )
    _notify(progress, f"Recognized {len(words)} timed words and {len(regions)} speech region(s)")
    return result
