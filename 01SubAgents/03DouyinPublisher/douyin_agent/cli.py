"""Command line entry point; all publishing actions use a local browser."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

from .jobs import event, fingerprint, load_job, metadata, now, submission_guard, write_json

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / ".local" / "browser-profile"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="抖音自动发布助手：选帧、做封面、填写发布页。")
    result.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="独立浏览器登录目录")
    result.add_argument("--channel", choices=("chrome", "chromium"), default="chrome")
    result.add_argument("--selectors", type=Path, help="页面改版时覆盖选择器的 JSON")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="检查运行环境")
    login_parser = sub.add_parser("login", help="打开抖音，扫码登录并保存在本地")
    login_parser.add_argument("--timeout", type=int, default=300)
    for name in ("prepare", "run"):
        command = sub.add_parser(name, help="生成封面和任务文件" if name == "prepare" else "生成后上传填写")
        command.add_argument("video", type=Path)
        command.add_argument("--copy-file", type=Path, help="导入 02 的 douyin-copy.json 文案和话题查询配置；显式参数优先")
        command.add_argument("--title", help="默认用视频文件名作为作品和封面标题")
        command.add_argument("--description", help="本条视频的简介；未提供时为空")
        command.add_argument("--hashtags", help="逗号分隔的话题，不含 #；传空字符串可取消")
        command.add_argument("--topic-query", action="append", default=[], help="按内容搜索相关平台话题；可重复，提供后启用选择")
        command.add_argument("--topic-count", type=int, default=None, help="目标真实话题总数，含已有话题；默认沿用导入值或 5，受平台可见上限约束")
        command.add_argument("--collection", help="优先选择此账号中名称完全匹配的合集")
        command.add_argument("--require-collection", action="store_true", help="没有匹配合集时阻止发布")
        command.add_argument("--declaration", help="自主声明的页面选项原文；由用户明确指定")
        command.add_argument("--font", help="本地中文字体文件")
        command.add_argument("--frame-time", type=float, help="指定封面截帧秒数")
        command.add_argument("--samples", type=int, default=16, help="自动抽样帧数，默认 16")
        command.add_argument("--title-mode", choices=("auto", "add", "preserve"), default="preserve",
                             help="默认保留画面；add 明确叠加黑白标题；auto 为 preserve 的兼容别名")
        command.add_argument("--cover-vertical", type=Path, help="复用已定稿的 3:4 竖封面，须同时传横封面")
        command.add_argument("--cover-horizontal", type=Path, help="复用已定稿的 4:3 横封面，须同时传竖封面")
        command.add_argument("--output", type=Path, help="任务输出目录")
        if name == "run":
            upload_arguments(command)
    upload = sub.add_parser("upload", help="读取 manifest.json 上传填写；--publish 自动提交")
    upload.add_argument("manifest", type=Path)
    upload_arguments(upload)
    return result


def upload_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--publish", action="store_true", help="确认所有必填项后自动点击发布")
    command.add_argument("--no-review", action="store_true", help="填写截图后关闭浏览器，不等待终端回车")
    command.add_argument("--timeout", type=int, default=600, help="上传处理最长等待秒数")


def _load_copy_file(path: Path) -> tuple[dict, dict]:
    """Validate the complete Stage 02 export before any output is created."""
    from .topics import validate_settings

    path = path.expanduser().resolve()
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError(f"无法读取 02 文案 JSON：{path}；{exc}") from exc
    required = ("title", "description", "hashtags", "topic_selection", "copy_source", "source_sha256")
    if not isinstance(data, dict) or any(key not in data for key in required):
        raise ValueError("02 文案须为含 title、description、hashtags、topic_selection、copy_source、source_sha256 的扁平对象；请重新运行 02 导出。")
    if not isinstance(data["title"], str) or not isinstance(data["description"], str):
        raise ValueError("02 文案的 title 和 description 必须是字符串。")
    if not isinstance(data["hashtags"], list) or any(not isinstance(tag, str) for tag in data["hashtags"]):
        raise ValueError("02 文案的 hashtags 必须是字符串列表。")
    if not isinstance(data["copy_source"], str) or not data["copy_source"].strip():
        raise ValueError("02 文案的 copy_source 必须是非空生成来源字符串。")
    if not isinstance(data["source_sha256"], str) or not re.fullmatch(r"[a-fA-F0-9]{64}", data["source_sha256"]):
        raise ValueError("02 文案的 source_sha256 必须是原片的 64 位十六进制 SHA256。")
    settings = data["topic_selection"]
    if not isinstance(settings, dict) or not isinstance(settings.get("enabled"), bool):
        raise ValueError("02 文案的 topic_selection 必须是对象，且 enabled 必须是布尔值。")
    settings = validate_settings(settings)
    fields = metadata(data["title"], data["description"], data["hashtags"])
    return {**data, **fields, "topic_selection": settings}, {
        "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
        # This is the original analyzed media, not necessarily the edited upload.
        "source_sha256": data["source_sha256"], "overrides": [],
    }


def prepare(args: argparse.Namespace) -> Path:
    from .media import prepare_covers

    video = args.video.expanduser().resolve()
    if not video.is_file():
        raise ValueError(f"视频不存在：{video}")
    if args.samples < 3 or args.samples > 64:
        raise ValueError("--samples 需要在 3–64 之间。")
    # Validate both finished covers before creating any output. This path does
    # not extract frames, crop the images, or apply a new title.
    from PIL import Image
    cover_inputs = {}
    for orientation, ratio in (("vertical", (3, 4)), ("horizontal", (4, 3))):
        value = getattr(args, f"cover_{orientation}", None)
        if value is not None:
            path = value.expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"封面不存在：{path}")
            try:
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    width, height = image.size
                    if image.format not in {"PNG", "JPEG", "WEBP"}:
                        raise ValueError("封面须为 PNG、JPEG 或 WEBP。")
                    if width * ratio[1] != height * ratio[0]:
                        raise ValueError(f"{orientation} 封面须为 {ratio[0]}:{ratio[1]} 比例。")
            except (OSError, SyntaxError) as exc:
                raise ValueError(f"无法读取封面：{path}") from exc
            cover_inputs[orientation] = path
    if len(cover_inputs) == 1:
        raise ValueError("--cover-vertical 和 --cover-horizontal 必须成对提供。")
    if args.require_collection and not args.collection:
        raise ValueError("--require-collection 需要同时指定 --collection。")
    from .topics import validate_settings
    queries = getattr(args, "topic_query", [])
    copy_file = getattr(args, "copy_file", None)
    imported, copy_provenance = _load_copy_file(copy_file) if copy_file is not None else ({}, None)
    if imported:
        title = args.title if args.title is not None else imported["title"]
        description = args.description if args.description is not None else imported["description"]
        tags = args.hashtags if args.hashtags is not None else imported["hashtags"]
        topic_settings = dict(imported["topic_selection"])
        copy_provenance["overrides"] = [key for key in ("title", "description", "hashtags")
                                         if getattr(args, key) is not None]
    else:
        # No subject, voice, hashtags, or account choices are assumed.
        title, description = args.title or video.stem, args.description
        tags = [] if queries and args.hashtags is None else args.hashtags
        topic_settings = {"enabled": False, "queries": [], "target_count": 5}
    fields = metadata(title, description, tags)
    if queries:
        # Explicit new queries can describe a different theme. Do not silently
        # filter them through old theme keywords, allowlists, or exclusions.
        topic_settings.update(enabled=True, queries=queries, keywords=queries,
                              allowed_names=[], exclude_keywords=[])
        topic_settings.pop("disabled_reason", None)
        if copy_provenance is not None:
            copy_provenance["overrides"].append("topic_query")
    if getattr(args, "topic_count", None) is not None:
        topic_settings["target_count"] = args.topic_count
        if copy_provenance is not None:
            copy_provenance["overrides"].append("topic_count")
    topic_settings = validate_settings(topic_settings)
    source = fingerprint(video)
    slug = re.sub(r"[^\w\u3400-\u9fff-]+", "-", fields["title"]).strip("-")[:40]
    output = args.output.expanduser().resolve() if args.output else ROOT / "runs" / f"{slug}-{source['sha256'][:12]}"
    manifest_path = output / "manifest.json"
    if (output / "publish-attempt.json").exists():
        raise ValueError("此输出目录曾经提交发布，请先核对抖音作品管理，勿覆盖发布记录。")
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("state") in {"published", "publish_attempted"}:
            raise ValueError("此任务已发布或提交，请先检查发布结果。")
    output.mkdir(parents=True, exist_ok=True)
    if cover_inputs:
        covers = {"source": "user_supplied", "warnings": [], "source_fingerprints": {}}
        for orientation, source_cover in cover_inputs.items():
            target = output / f"cover-{orientation}{source_cover.suffix.lower()}"
            if source_cover != target:
                shutil.copyfile(source_cover, target)
            covers[orientation] = str(target)
            covers["source_fingerprints"][orientation] = fingerprint(target)
        print("已复用定稿双封面，未抽帧、裁切或叠加标题。", flush=True)
    else:
        print("正在抽帧和生成封面…", flush=True)
        covers = prepare_covers(video, fields["title"], output, font_path=args.font,
                                frame_time=args.frame_time, samples=args.samples, title_mode=args.title_mode)
    job = {
        "schema_version": 1, "job_id": source["sha256"][:16], "created_at": now(),
        "video": str(video), "source": source, **fields,
        "collection": {"preferred": args.collection, "keywords": [],
                       "required": args.require_collection},
        "declaration": args.declaration, "covers": covers,
        "output_dir": str(output), "state": "prepared", "history": [],
        "copy_source": imported["copy_source"] if imported else "user_fields_or_filename; not_transcribed",
    }
    if copy_provenance is not None:
        job["copy_provenance"] = copy_provenance
    if topic_settings["enabled"] or imported:
        job["topic_selection"] = topic_settings
    event(job, "prepared")
    write_json(manifest_path, job)
    print(f"封面：{covers['vertical']}\n横封面：{covers['horizontal']}\n任务：{manifest_path}")
    for warning in covers.get("warnings", []):
        print(f"提示：{warning}")
    return manifest_path


def upload(args: argparse.Namespace, manifest_path: Path) -> int:
    from .browser import run_publish

    manifest_path = manifest_path.expanduser().resolve()
    job = load_job(manifest_path)
    settings = json.loads(args.selectors.read_text(encoding="utf-8")) if args.selectors else {}
    settings["review"] = not args.no_review
    settings["_manifest_path"] = str(manifest_path)
    profile = args.profile.expanduser().resolve()

    def execute() -> dict:
        return run_publish(job, profile, publish=args.publish, channel=args.channel,
                           timeout_seconds=args.timeout, selector_config=settings)

    result: dict = {}
    try:
        if args.publish:
            with submission_guard(profile, job) as record:
                result = execute()
                record.update(result)
        else:
            result = execute()
    except Exception:
        if (Path(job["output_dir"]) / "publish-attempt.json").exists():
            event(job, "publish_attempted", {"message": "提交结果未知；核对作品管理后再操作。"})
            write_json(manifest_path, job)
        raise
    status = result.get("status", "failed")
    if status != "published" and (Path(job["output_dir"]) / "publish-attempt.json").exists():
        status = "publish_attempted"
    event(job, status, result)
    write_json(manifest_path, job)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status in {"filled", "published"} else 1


def doctor(channel: str = "chrome") -> int:
    import importlib.util

    checks = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    checks.update({name: bool(importlib.util.find_spec(name)) for name in ("PIL", "numpy", "cv2", "playwright")})
    checks["chrome"] = Path("/Applications/Google Chrome.app").exists() or shutil.which("google-chrome")
    if channel == "chromium" and checks["playwright"]:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            checks["selected_browser"] = Path(playwright.chromium.executable_path).is_file()
    else:
        checks["selected_browser"] = bool(checks["chrome"])
    checks["channel"] = channel
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if all(checks[name] for name in ("ffmpeg", "ffprobe", "PIL", "numpy", "cv2", "playwright", "selected_browser")) else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor(args.channel)
        if args.command == "login":
            from .browser import login
            report = login(args.profile.expanduser().resolve(), channel=args.channel, timeout_seconds=args.timeout)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("status") in {"logged_in", "authenticated"} else 1
        if args.command in {"prepare", "run"}:
            path = prepare(args)
            return upload(args, path) if args.command == "run" else 0
        return upload(args, args.manifest)
    except KeyboardInterrupt:
        print("\n已停止。若刚才点击过发布，请先到作品管理核对结果。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
