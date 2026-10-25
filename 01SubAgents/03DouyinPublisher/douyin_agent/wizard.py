"""Small Terminal launcher for users who prefer dropping a video path."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from .cli import main, parser, prepare, upload


def video_path() -> Path:
    raw = input("把视频拖进此窗口，再按回车：\n> ").strip()
    path = Path(raw).expanduser()
    if path.is_file():
        return path.resolve()
    try:
        parts = shlex.split(raw)
    except ValueError:
        parts = []
    if len(parts) == 1 and Path(parts[0]).expanduser().is_file():
        return Path(parts[0]).expanduser().resolve()
    raise ValueError("没有找到该视频文件。请拖入一条视频。")


def wizard() -> int:
    print("\n抖音发布助手\n")
    print("1. 测试生成封面（默认）\n2. 登录抖音\n3. 生成封面并上传，停在发布页检查\n4. 生成封面、上传并自动发布\n")
    mode = input("选择 1/2/3/4 [1]：").strip() or "1"
    if mode == "2":
        return main(["login"])
    if mode not in {"1", "3", "4"}:
        raise ValueError("请输入 1、2、3 或 4。")
    video = video_path()
    title = input(f"作品及封面标题 [默认：{video.stem}]：").strip() or video.stem
    command = "prepare" if mode == "1" else "run"
    arguments = [command, str(video), "--title", title]
    if mode == "4":
        arguments.append("--publish")
    if mode != "1":
        collection = input("指定此账号的合集名称（留空不选择合集）：").strip()
        if collection:
            arguments.extend(["--collection", collection, "--require-collection"])
    args = parser().parse_args(arguments)
    path = prepare(args)
    print(f"\n可在此文件里修改简介和话题：{path}")
    if mode == "1":
        if sys.platform == "darwin":
            subprocess.run(["open", str(path.parent / "cover-vertical.jpg")], check=False)
        return 0
    return upload(args, path)


if __name__ == "__main__":
    try:
        result = wizard()
    except (KeyboardInterrupt, EOFError):
        print("\n已停止。")
        result = 130
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        result = 1
    if sys.stdin.isatty():
        input("\n按回车关闭窗口。")
    raise SystemExit(result)
