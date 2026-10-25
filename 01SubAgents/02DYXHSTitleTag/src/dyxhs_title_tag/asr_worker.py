"""Optional ASR subprocess; runs in the user's already configured environment."""
from __future__ import annotations
import json
import sys


def main() -> None:
    audio, model, backend = sys.argv[1:]
    if backend == "mlx":
        import mlx_whisper
        result = mlx_whisper.transcribe(audio, path_or_hf_repo=model, language="zh",
                                        condition_on_previous_text=False, temperature=0.0,
                                        verbose=None)
        segments = result.get("segments", [])
    elif backend == "faster-whisper":
        from faster_whisper import WhisperModel
        recognizer = WhisperModel(model, device="cpu", compute_type="int8", local_files_only=True)
        rows, _ = recognizer.transcribe(audio, language="zh", vad_filter=True,
                                        condition_on_previous_text=False, temperature=0.0)
        segments = [{"start": item.start, "end": item.end, "text": item.text,
                     "no_speech_prob": item.no_speech_prob} for item in rows]
    else:
        raise ValueError("ASR backend must be mlx or faster-whisper")
    print(json.dumps({"backend": backend, "segments": segments}, ensure_ascii=False))


if __name__ == "__main__":
    main()
