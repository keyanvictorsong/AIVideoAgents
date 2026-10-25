"""Command-line entry point for single videos and reusable batch processing."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .pipeline import edit_folder, edit_video


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trim talking-head pauses and fillers, generate captions and highlighted MP4s locally.")
    parser.add_argument("input", type=Path, help="Video file or folder containing videos")
    parser.add_argument("--output", "-o", type=Path, help="Output folder; one video defaults to adjacent <stem>_output")
    parser.add_argument("--config", type=Path, help="JSON configuration (see config.example.json)")
    parser.add_argument("--backend", choices=["auto", "mlx", "faster-whisper"])
    parser.add_argument("--model", help="Local model directory or backend-specific model name")
    parser.add_argument("--language", help="Language code (default zh); 'auto' to detect")
    parser.add_argument("--title", help="Optional title displayed at the top of the video")
    parser.add_argument("--analyze-only", action="store_true", help="Write transcript, captions and edit plan without rendering")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing generated outputs, retaining reusable ASR cache")
    parser.add_argument("--retranscribe", action="store_true", help="Ignore the existing transcript cache")
    parser.add_argument("--plan", type=Path, help="Render a reviewed edit plan for one video (requires matching source hash)")
    parser.add_argument("--no-clean-video", action="store_true", help="Export only the captioned MP4")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.output is None:
        if args.input.is_dir():
            parser.error("Folder inputs require --output; a single video gets an adjacent output folder automatically")
        args.output = args.input.with_name(args.input.stem + "_output")
    try:
        config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
        if not isinstance(config, dict):
            raise ValueError("Configuration must be a JSON object")
        for key in ("backend", "model", "language"):
            value = getattr(args, key)
            if value is not None:
                config[key] = value
        if args.no_clean_video:
            config["clean_video"] = False
        if args.title is not None:
            config.setdefault("captions", {})["title"] = args.title
        kwargs = dict(config=config, overwrite=args.overwrite, retranscribe=args.retranscribe,
                      analyze_only=args.analyze_only, progress=lambda text: print(text, flush=True))
        if args.input.is_dir():
            if args.plan:
                parser.error("--plan is only valid with one input video")
            result = edit_folder(args.input, args.output, **kwargs)
            return 1 if result["failed"] else 0
        edit_video(args.input, args.output, plan_path=args.plan, **kwargs)
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
