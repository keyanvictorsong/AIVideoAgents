#!/usr/bin/env python3
"""Create an empty recipient-owned workspace; copy only explicitly given media."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="新建任务；复制明确指定的视频，保留原文件。")
    parser.add_argument("name", help="任务名，仅字母、数字、中文、连字符或下划线")
    parser.add_argument("--video", type=Path, help="可选：复制自己的一个视频到 00Input")
    args = parser.parse_args()
    if not re.fullmatch(r"[\w-]{1,80}", args.name) or args.name.startswith("."):
        parser.error("任务名格式不正确。")
    video = args.video.expanduser().resolve() if args.video else None
    if video is not None and (not video.is_file() or video.suffix.lower() not in {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}):
        parser.error("--video 必须是实际视频文件。")
    task = ROOT / "02VideoWorkSpace" / args.name
    if task.exists():
        parser.error("同名任务已存在；不会覆盖。")
    task.mkdir(parents=True)
    for name in ("00Input", "01Edit", "02Copy", "03Douyin", "04Xiaohongshu", "records"):
        (task / name).mkdir()
    source = None
    if video is not None:
        target = task / "00Input" / video.name
        shutil.copyfile(video, target)
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        source = {"path": str(target.relative_to(task)), "sha256": digest.hexdigest()}
    record = {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source, "video_type": None, "target_platforms": [],
        "style": None, "title_selection": None, "publish_authorization": None,
        "note": "请本人指定内容类型、风格、目标平台与本次发布授权。",
    }
    (task / "task.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(task)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
