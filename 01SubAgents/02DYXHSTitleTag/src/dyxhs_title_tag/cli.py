"""Local commands; generation never publishes or silently substitutes mock data."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys

from .config import ROOT, Settings
from .pipeline import collect_all, run


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="视频字幕 → 抖音/小红书参考分析与文案，支持无字幕时提供描述")
    result.add_argument("--env-file", type=Path, help="可选模型配置；助手审核方式无需 API")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="检查本地依赖与配置状态，不输出密钥")
    analyze = sub.add_parser("analyze", help="提取字幕和画面证据")
    task = sub.add_parser("run", help="可恢复的分析、采集与文案导出流程")
    for item in (analyze, task):
        item.add_argument("video", type=Path)
        item.add_argument("--output", "-o", type=Path)
        item.add_argument("--content", help="没有字幕时，提供视频实际内容描述")
        item.add_argument("--content-file", type=Path, help="从 UTF-8 文本读取视频描述")
        item.add_argument("--frame-interval", type=float, default=2)
        item.add_argument("--skip-ocr", action="store_true", help="已提供描述/文字字幕时跳过画面 OCR")
        item.add_argument("--refresh", action="store_true", help="重新提取视频证据")
    task.add_argument("--reviewed-facts", type=Path, help="当前助手核对后的事实 JSON")
    task.add_argument("--reviewed-copy", type=Path, help="当前助手生成的文案 JSON")
    task.add_argument("--collection-dir", type=Path, help="复用实际采集记录所在目录，保留原始采样时间")
    task.add_argument("--skip-collect", action="store_true")
    collect = sub.add_parser("collect", help="独立验证平台采集能力")
    collect.add_argument("--output", "-o", type=Path, required=True)
    for item in (task, collect):
        item.add_argument("--query", action="append", help="可重复的相关检索词")
        item.add_argument("--import-references", type=Path, action="append", default=[])
        item.add_argument("--target", type=int, default=60)
        item.add_argument("--max-pages", type=int, default=5)
        item.add_argument("--headed", action="store_true", help="显示本工具的独立浏览器")
    login = sub.add_parser("login", help="打开独立采集会话，手动完成平台登录")
    login.add_argument("platform", choices=["douyin", "xiaohongshu"])
    return result


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    try:
        settings = Settings.load(args.env_file)
        if args.command == "doctor":
            print(json.dumps({"ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"),
                              "swiftc": shutil.which("swiftc"), "tesseract": shutil.which("tesseract"),
                              "browser_installed": importlib.util.find_spec("playwright") is not None,
                              "model_configured": settings.model_configured,
                              "api_key_present": bool(settings.api_key),
                              "mode_without_api": "字幕/描述提取 + 当前助手审核事实和文案"}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "login":
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                raise ValueError("请先安装采集依赖：python -m pip install -e '.[browser]'") from None
            profile = ROOT / ".local" / "profiles" / args.platform
            with sync_playwright() as p:
                try:
                    context = p.chromium.launch_persistent_context(str(profile), headless=False)
                except Exception:
                    context = p.chromium.launch_persistent_context(str(profile), channel="chrome", headless=False)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto("https://www.douyin.com" if args.platform == "douyin" else "https://www.xiaohongshu.com/explore")
                    input("请在独立浏览器内完成登录，完成后回到终端按回车保存会话：")
                finally:
                    context.close()
            return 0
        if args.command == "collect":
            if not args.query and not args.import_references:
                argument_parser.error("collect 至少需要 --query 或 --import-references")
            collection = collect_all(args.query or [], args.output.resolve(), settings, target=args.target,
                                     max_pages=args.max_pages, headless=not args.headed, imports=args.import_references)
            return 0 if all(r["status"] == "ok" for r in collection.values()) else 2
        video = args.video.expanduser().resolve()
        output = args.output.expanduser().resolve() if args.output else ROOT / "output" / video.stem
        if args.content and args.content_file:
            argument_parser.error("--content 和 --content-file 只选一个")
        content = args.content_file.read_text(encoding="utf-8") if args.content_file else args.content
        kwargs = dict(content=content, frame_interval=args.frame_interval, skip_ocr=args.skip_ocr, refresh=args.refresh)
        if args.command == "analyze":
            from .video import analyze_video
            from .pipeline import add_user_description, write_json
            analysis = analyze_video(video, output / "analysis", frame_interval=args.frame_interval, ocr=not args.skip_ocr, refresh=args.refresh)
            if content:
                analysis = add_user_description(analysis, content)
                write_json(output / "analysis" / "video_analysis.json", analysis)
            print(f"识别完成：{output / 'analysis' / 'transcript.txt'}")
            return 0
        result = run(video, output, settings, reviewed_facts=args.reviewed_facts, reviewed_copy=args.reviewed_copy,
                     collection_dir=args.collection_dir, queries=args.query, imports=args.import_references,
                     target=args.target, max_pages=args.max_pages, headless=not args.headed,
                     skip_collect=args.skip_collect, **kwargs)
        print(f"状态：{result['status']}\n输出目录：{output}")
        return 0 if result["status"] == "complete" else 2
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
