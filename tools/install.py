#!/usr/bin/env python3
"""Install bundled local sources; never read the sender's machine configuration."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = [
    "01SubAgents/01TalkingEditor/00Code",
    "01SubAgents/02DYXHSTitleTag",
    "01SubAgents/03DouyinPublisher",
    "01SubAgents/04XiaohongShuPublisher",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="安装四个主 agent；安装会下载开源依赖和浏览器。")
    parser.add_argument("--with-optional", action="store_true", help="同时安装旧版参考采集工具")
    parser.add_argument("--skip-browser", action="store_true", help="不下载 Chromium；可改用已安装 Chrome")
    parser.add_argument("--dry-run", action="store_true", help="仅显示计划，不写入或联网")
    args = parser.parse_args()
    if os.name == "nt":
        print("完整发布流程目前需要 macOS/Linux（发布器使用 POSIX 文件锁）。Windows 可单独安装剪辑/文案模块，见各目录说明。", file=sys.stderr)
        return 1
    if not (3, 11) <= sys.version_info[:2] < (3, 14):
        print("请使用 Python 3.11、3.12 或 3.13（剪辑包的版本要求）。", file=sys.stderr)
        return 1
    projects = list(PROJECTS)
    if args.with_optional:
        projects.append("01SubAgents/99Optional/03XHSGuider")
    for project in projects:
        if not (ROOT / project / "pyproject.toml").is_file():
            print(f"缺少源码：{project}", file=sys.stderr)
            return 1
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    editor_extra = ".[mac]" if platform.system() == "Darwin" and platform.machine() == "arm64" else "."
    if args.dry_run:
        print("创建或复用包内 .venv；不会复制其他机器的虚拟环境。")
        for index, project in enumerate(projects):
            extra = editor_extra if index == 0 else ".[browser]" if index == 1 else "."
            print(f"在 {project} 安装本地项目：pip install -e {extra}")
        print("Chromium：" + ("跳过下载" if args.skip_browser else "通过 Playwright 下载"))
        return 0
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
    try:
        subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip", "setuptools"], check=True)
        for index, project in enumerate(projects):
            extra = editor_extra if index == 0 else ".[browser]" if index == 1 else "."
            subprocess.run([str(python), "-m", "pip", "install", "-e", extra], cwd=ROOT / project, check=True)
        if not args.skip_browser:
            subprocess.run([str(python), "-m", "playwright", "install", "chromium"], check=True)
    except subprocess.CalledProcessError as exc:
        print(f"安装未完成（退出码 {exc.returncode}）。修复网络或依赖后可重新运行。", file=sys.stderr)
        return exc.returncode
    print("安装完成。运行 python3 tools/doctor.py 检查 FFmpeg 和模块。")
    print("本脚本不会登录平台或上传视频。首次 ASR 可能另行下载模型。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
