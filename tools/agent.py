#!/usr/bin/env python3
"""Run a bundled agent from any current directory using the local environment."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
AGENTS = {
    "edit": "talking_editor",
    "deliver": "talking_editor.delivery",
    "copy": "dyxhs_title_tag",
    "douyin": "douyin_agent",
    "xhs": "xhs_publisher",
    "research": "dyxhs_guider",
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print("用法：python3 tools/agent.py <edit|deliver|copy|douyin|xhs|research> [该 agent 的参数]")
        print("例如：python3 tools/agent.py douyin --help")
        print("research 是可选旧工具；先用 tools/install.py --with-optional 安装。")
        return 0
    module = AGENTS.get(sys.argv[1])
    if module is None:
        print("未知 agent。可选：" + ", ".join(AGENTS), file=sys.stderr)
        return 2
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print("尚未安装。请先运行：python3 tools/install.py", file=sys.stderr)
        return 1
    # Keep the caller's working directory so relative video/output paths work.
    return subprocess.call([str(python), "-m", module, *sys.argv[2:]])


if __name__ == "__main__":
    raise SystemExit(main())
