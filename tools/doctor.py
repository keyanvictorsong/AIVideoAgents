#!/usr/bin/env python3
"""Report local readiness without opening a browser, reading keys or networking."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.is_file() and Path(sys.executable).absolute() != python.absolute():
        return subprocess.call([str(python), str(Path(__file__).resolve())])
    modules = ("talking_editor", "dyxhs_title_tag", "douyin_agent", "xhs_publisher", "faster_whisper", "PIL", "cv2", "playwright")
    checks = {name: importlib.util.find_spec(name) is not None for name in modules}
    report = {
        "python_supported": (3, 11) <= sys.version_info[:2] < (3, 14),
        "bundle_environment_installed": python.is_file(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "modules": checks,
        "note": "未检查账户、密钥或平台页面；未打开浏览器。",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["python_supported"] and report["bundle_environment_installed"] and report["ffmpeg"] and report["ffprobe"] and all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
