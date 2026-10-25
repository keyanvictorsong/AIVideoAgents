"""Prepare a local Xiaohongshu upload; publishing always remains a user action."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .jobs import (
    ATTEMPT_FILENAME, BLOCKED_STATES, PLATFORM, event, fingerprint, load_job,
    metadata, now, preparation_guard, profile_lock, validate_cover, write_json,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / ".local" / "browser-profile"


def _saved_settings() -> dict:
    settings_path = ROOT / ".local" / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"本地设置不是有效的 JSON：{settings_path}") from exc
    if not isinstance(settings, dict):
        raise ValueError(f"本地设置必须是 JSON 对象：{settings_path}")
    return settings


def default_profile() -> Path:
    saved = _saved_settings().get("browser_profile")
    if not isinstance(saved, str) or not saved.strip():
        return DEFAULT_PROFILE
    profile = Path(saved).expanduser()
    return (profile if profile.is_absolute() else ROOT / profile).resolve()


def selector_settings(override: Path | None = None) -> dict:
    settings: dict = {}
    default_path = ROOT / ".local" / "selectors.json"
    paths = ([default_path] if default_path.is_file() else [])
    if override is not None:
        paths.append(override.expanduser())
    for path in paths:
        current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            raise ValueError(f"选择器配置必须是 JSON 对象：{path}")
        settings.update(current)
    return settings


def positive_seconds(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("超时秒数必须大于 0。")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="小红书视频发布准备：上传、填写文案、设置已有封面，保持页面打开供用户手动发布。",
        allow_abbrev=False,
    )
    result.add_argument("--profile", type=Path, help="本人专用的持久浏览器 profile；优先于 manifest 和默认目录")
    result.add_argument("--channel", choices=("chrome", "chromium"), default="chrome")
    result.add_argument("--selectors", type=Path, help="覆盖已保存选择器的 JSON；放在子命令前，未覆盖键仍保留")
    sub = result.add_subparsers(dest="command", required=True)
    login_command = sub.add_parser("login", help="打开专用浏览器，由本人完成登录", allow_abbrev=False)
    login_command.add_argument("--timeout", type=positive_seconds, default=300)
    prepare_command = sub.add_parser("prepare", help="校验已有素材并生成 manifest，不打开浏览器", allow_abbrev=False)
    prepare_command.add_argument("video", type=Path)
    prepare_command.add_argument("--cover", type=Path, required=True, help="已有的 3:4 竖版自定义封面")
    prepare_command.add_argument("--title", required=True, help="已确认的发布标题，最多 20 字")
    prepare_command.add_argument("--description", required=True, help="已确认的简介，可传空字符串")
    prepare_command.add_argument("--tags", default="", help="逗号或空格分隔的话题，可含 #")
    prepare_command.add_argument("--output", type=Path, help="本条视频的任务输出目录")
    upload_command = sub.add_parser("upload", help="上传并填写后保持浏览器打开，交给用户亲自发布", allow_abbrev=False)
    upload_command.add_argument("manifest", type=Path)
    upload_command.add_argument("--timeout", type=positive_seconds, default=600)
    return result


def prepare(args: argparse.Namespace) -> Path:
    video = args.video.expanduser().resolve()
    if not video.is_file():
        raise ValueError(f"视频不存在：{video}")
    fields = metadata(args.title, args.description, args.tags)
    cover = args.cover.expanduser().resolve()
    cover_info = validate_cover(cover)
    source = fingerprint(video)
    slug = re.sub(r"[^\w\u3400-\u9fff-]+", "-", fields["title"]).strip("-") or "video"
    output = args.output.expanduser().resolve() if args.output else ROOT / "runs" / f"{slug}-{source['sha256'][:12]}"
    manifest_path = output / "manifest.json"
    if (output / ATTEMPT_FILENAME).exists() or (output / "publish-attempt.json").exists():
        raise ValueError("此目录已有上传或人工交接记录，请先只读核对发布结果，不能覆盖。")
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or previous.get("state") in BLOCKED_STATES:
            raise ValueError("此任务已有上传、人工交接或发布状态，不能覆盖；请先只读核对结果。")
    job = {
        "schema_version": 1, "platform": PLATFORM, "job_id": source["sha256"][:16],
        "created_at": now(), "source": source, "video": str(video), **fields,
        "cover": str(cover), "cover_source": cover_info, "output_dir": str(output),
        "browser_profile": str((args.profile or default_profile()).expanduser().resolve()),
        "state": "prepared", "history": [],
    }
    event(job, "prepared")
    write_json(manifest_path, job)
    print(f"任务：{manifest_path}\n封面：{cover}")
    return manifest_path


def _report_state(result: dict) -> str:
    status = result.get("status")
    if status in {"filled", "awaiting_user_publish"}:
        return "awaiting_user_publish"
    if status in {"published", "publish_attempted", "manual_publication_unverified"}:
        return "manual_publication_unverified"
    return "preparation_interrupted"


def upload(args: argparse.Namespace, manifest_path: Path) -> int:
    # Fail before importing the browser module: background jobs cannot provide a
    # reliable manual handoff, and must not launch a browser or upload anything.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("upload 需要交互式终端（TTY）交接；请在本机终端运行，最终发布由本人手动点击。")
    manifest_path = manifest_path.expanduser().resolve()
    job = load_job(manifest_path)
    settings = selector_settings(args.selectors)
    selected_profile = args.profile or job.get("browser_profile") or default_profile()
    profile = Path(selected_profile).expanduser().resolve()
    from .browser import run_prepare

    result: dict = {}
    with preparation_guard(profile, job) as record:
        event(job, "preparing")
        write_json(manifest_path, job)
        try:
            result = run_prepare(job, profile, channel=args.channel,
                                 timeout_seconds=args.timeout, selector_config=settings)
            if not isinstance(result, dict):
                raise ValueError("浏览器未返回有效的准备结果；请只读核对平台页面。")
            status = _report_state(result)
            if status == "preparation_interrupted" and not (Path(job["output_dir"]) / ATTEMPT_FILENAME).exists():
                status = "preparation_failed"
            record.update(status=status)
            # Do not store arbitrary browser data: a browser profile can contain
            # authentication details, and the manifest is an ordinary artifact.
            details = {"status": status, "publication_verified": False}
            event(job, status, details)
            write_json(manifest_path, job)
        except BaseException:
            upload_started = (Path(job["output_dir"]) / ATTEMPT_FILENAME).exists()
            event(job, "preparation_interrupted" if upload_started else "preparation_failed", {
                "message": ("上传或人工交接中断；先只读检查平台，禁止自动重试。" if upload_started
                            else "视频上传前准备失败，可修正登录或页面问题后重试。"),
                "publication_verified": False,
            })
            write_json(manifest_path, job)
            raise
    print(json.dumps({"status": status, "publication_verified": False,
                      "manifest": str(manifest_path)}, ensure_ascii=False, indent=2))
    return 0 if status == "awaiting_user_publish" else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args)
            return 0
        if args.command == "login":
            from .browser import login
            profile = (args.profile or default_profile()).expanduser().resolve()
            creator_url = _saved_settings().get("creator_url")
            login_options = {"channel": args.channel, "timeout_seconds": args.timeout}
            if isinstance(creator_url, str) and creator_url.strip():
                login_options["upload_url"] = creator_url.strip()
            with profile_lock(profile):
                report = login(profile, **login_options)
            status = report.get("status", "unknown") if isinstance(report, dict) else "unknown"
            print(json.dumps({"status": status}, ensure_ascii=False))
            return 0 if status in {"logged_in", "authenticated"} else 1
        return upload(args, args.manifest)
    except KeyboardInterrupt:
        print("\n已停止。上传或交接一旦开始，请先只读核对小红书页面与作品管理。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
