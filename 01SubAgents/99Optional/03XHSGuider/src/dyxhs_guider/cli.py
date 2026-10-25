"""Collect real references, prepare an evidence review, and render local HTML."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile

from .collectors import DEFAULT_QUERIES, ROOT, TITLE_TAG, collect_platform, login, read_payload
from .core import normalize_rows, select_samples, validate_analyses
from .report import render_report

PLATFORMS = ("douyin", "xiaohongshu")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="抖音/小红书公开视频参考 → 证据分析 → HTML")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="只检查本地环境，不执行平台搜索")
    log = sub.add_parser("login", help="在专用采集窗口内手动登录")
    log.add_argument("platform", choices=PLATFORMS)
    collect = sub.add_parser("collect", help="只采集真实平台数据并保存状态")
    run = sub.add_parser("run", help="采集、筛选、准备分析、导出 HTML")
    prepare = sub.add_parser("prepare", help="导入已有数据、筛选并准备助手分析文件")
    render = sub.add_parser("render", help="合并审核分析并重新生成 HTML")
    for c in (collect, run, prepare, render):
        c.add_argument("--output", "-o", type=Path, default=ROOT / "output" / "research")
    for c in (collect, run):
        c.add_argument("--platform", choices=["both", *PLATFORMS], default="both")
        c.add_argument("--query", action="append")
        c.add_argument("--candidate-target", type=int, default=100, help="每平台候选目标，采集成功不等于入选")
        c.add_argument("--max-pages", type=int, default=6)
        c.add_argument("--headed", action="store_true")
        c.add_argument("--mcp-url", help="可选已登录小红书 MCP 服务地址")
    for c in (run, prepare):
        c.add_argument("--import-references", "--import", type=Path, action="append", default=[])
        c.add_argument("--total", type=int, default=50)
        c.add_argument("--douyin-quota", type=int)
        c.add_argument("--xiaohongshu-quota", type=int)
        c.add_argument("--min-douyin-likes", type=int, default=10000)
        c.add_argument("--min-xiaohongshu-likes", type=int, default=1000)
        c.add_argument("--max-per-author", type=int, default=3)
        c.add_argument("--title", default="公开视频参考研究")
        c.add_argument("--keyword", action="append", default=[], help="显式限定内容关键词；可重复。不填则不按题材过滤")
    for c in (collect, run, prepare):
        c.add_argument("--days", type=int, default=90, help="发布时间窗口；经典样本需显式扩大并记录")
    run.add_argument("--skip-collect", action="store_true", help="只复用当前批次采集与导入数据")
    for c in (run, prepare, render):
        c.add_argument("--analyses", type=Path, help="助手审核后的逐条分析 JSON")
    return p


def _collect(args, output: Path) -> dict:
    if not args.query:
        raise ValueError("采集需要至少一个 --query；分享版不预置题材或个人检索词")
    platforms = PLATFORMS if args.platform == "both" else (args.platform,)
    status = {}
    for platform in platforms:
        print(f"采集 {platform}…", flush=True)
        payload = collect_platform(platform, output=output / "collection", queries=args.query or DEFAULT_QUERIES,
                                   target=args.candidate_target, days=args.days, max_pages=args.max_pages,
                                   headed=args.headed, mcp_url=args.mcp_url)
        status[platform] = {k: v for k, v in payload.items() if k != "samples"}
        print(f"{platform}: {payload['status']}，原始 {len(payload['samples'])} 条", flush=True)
    write_json(output / "collection_status.json", status)
    return status


def _load_analyses(path: Path | None):
    if path is None:
        return []
    payload = json.loads(path.expanduser().read_text(encoding="utf-8-sig"))
    if not isinstance(payload, (list, dict)):
        raise ValueError("分析文件须为数组或包含 analyses 的 JSON 对象")
    if isinstance(payload, dict) and "analyses" in payload and not isinstance(payload["analyses"], list):
        raise ValueError("分析文件 analyses 须为数组")
    return payload


def _selection_options(args) -> dict:
    quotas = None
    if args.douyin_quota is not None or args.xiaohongshu_quota is not None:
        dy = args.douyin_quota if args.douyin_quota is not None else args.total - args.xiaohongshu_quota
        xhs = args.xiaohongshu_quota if args.xiaohongshu_quota is not None else args.total - dy
        quotas = {"douyin": dy, "xiaohongshu": xhs}
    return {"total": args.total, "quotas": quotas, "recent_days": args.days,
            "min_likes": {"douyin": args.min_douyin_likes, "xiaohongshu": args.min_xiaohongshu_likes},
            "max_per_author": args.max_per_author, "topic_keywords": args.keyword}


def _preflight(args, output: Path) -> None:
    """Reject bad explicit inputs before running collectors or writing outputs."""
    select_samples([], **_selection_options(args))
    for path in args.import_references:
        path = path.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"导入文件不存在：{path}")
        if not isinstance(read_payload(path), (list, dict)):
            raise ValueError(f"参考文件须为 JSON 数组、对象或 CSV：{path}")
    analyses = args.analyses
    if analyses is None and (output / "analysis.json").is_file():
        analyses = output / "analysis.json"
    _load_analyses(analyses)


def _finish(result: dict, output: Path, analyses_path: Path | None, *, pending_outputs: dict | None = None) -> dict:
    if not isinstance(result, dict) or not isinstance(result.get("samples"), list):
        raise ValueError("报告数据缺少 samples 数组")
    if type(result.get("target_total")) is not int or result["target_total"] < 1:
        raise ValueError("报告数据缺少有效 target_total")
    result = deepcopy(result)
    if analyses_path is None:
        existing = output / "analysis.json"
        analyses_path = existing if existing.is_file() else None
    reviewed = _load_analyses(analyses_path)
    validation = validate_analyses(result["samples"], reviewed)
    validated = validation.get("analyses", [])
    if isinstance(validated, dict):
        index = validated
    else:
        index = {a.get("sample_id", f"{a.get('platform')}:{a.get('id')}"): a for a in validated}
    for row in result["samples"]:
        row["analysis"] = index.get(row["sample_id"], {})
    result["analysis_status"] = validation
    unresolved_duplicates = any(item.get("status") == "needs_review"
                                for item in result.get("cross_platform_duplicate_candidates", []))
    quotas = result.get("quotas")
    counts_match = len(result["samples"]) == result["target_total"]
    if isinstance(quotas, dict):
        counts_match = counts_match and sum(quotas.values()) == result["target_total"] and all(
            sum(row.get("platform") == platform for row in result["samples"]) == quota
            for platform, quota in quotas.items())
    else:
        counts_match = False
    result["status"] = "complete" if (result.get("selection_status") == "complete"
                                          and validation.get("status") == "complete"
                                          and counts_match
                                          and not unresolved_duplicates) else "partial"
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    # Parse, validate and render completely before replacing any prior artifact.
    # A malformed input or rendering failure therefore leaves the prior report.
    with tempfile.TemporaryDirectory(prefix=".guider-stage-", dir=output) as directory:
        staging = Path(directory)
        artifacts = dict(pending_outputs or {})
        artifacts.update({
            "analysis.json": reviewed, "analysis_validation.json": validation,
            "samples.json": {"samples": [{k: v for k, v in s.items() if k != "analysis"} for s in result["samples"]]},
            "report_data.json": result,
        })
        for name, value in artifacts.items():
            write_json(staging / name, value)
        render_report(result, staging / "report.html")
        for path in staging.iterdir():
            path.replace(output / path.name)
    return result


def _prepare(args, output: Path) -> dict:
    rows, sources = [], []
    status_file = output / "collection_status.json"
    status = read_payload(status_file) if status_file.is_file() else {}
    if not isinstance(status, dict):
        raise ValueError("collection_status.json 须为平台状态对象")
    imported_paths = [p.expanduser().resolve() for p in args.import_references]
    for path in imported_paths:
        if not path.is_file():
            raise FileNotFoundError(f"导入文件不存在：{path}")
    collection_paths = [output / "collection" / f"{platform}.json" for platform in PLATFORMS]
    paths = collection_paths + imported_paths
    for path in dict.fromkeys(paths):
        if not path.is_file():
            continue
        payload = read_payload(path)
        if not isinstance(payload, (list, dict)):
            raise ValueError(f"参考文件须为 JSON 数组、对象或 CSV：{path}")
        normalized = normalize_rows(payload)
        for row in normalized:
            row.setdefault("source_file", str(path))
        rows.extend(normalized)
        sources.append({"path": str(path), "rows": len(normalized)})
        if (isinstance(payload, dict) and payload.get("platform") in PLATFORMS and payload.get("status")
                and (path in collection_paths or (payload["platform"] not in status
                     and not any(row.get("source_type") == "public_web_search" for row in normalized)))):
            status[payload["platform"]] = {k: v for k, v in payload.items() if k != "samples"}
    selected = select_samples(rows, **_selection_options(args))
    for platform in PLATFORMS:
        platform_rows = [row for row in rows if row.get("platform") == platform]
        direct_ids = {row["sample_id"] for row in platform_rows if row.get("source_type") != "public_web_search"
                      and (row.get("source_type") in {"browser", "browser_response", "xiaohongshu_mcp"}
                           or row.get("source_file") in {str(path) for path in collection_paths})}
        public_ids = {row["sample_id"] for row in platform_rows if row.get("source_type") == "public_web_search"} - direct_ids
        all_ids = {row["sample_id"] for row in platform_rows}
        imported_ids = all_ids - direct_ids - public_ids
        entry = status.setdefault(platform, {"status": "not_started", "reason": "本批次没有平台入口采集记录。"})
        if not isinstance(entry, dict):
            raise ValueError(f"{platform} 的采集状态须为对象")
        entry.update(candidate_count=len(all_ids), selected_count=sum(row["platform"] == platform for row in selected["samples"]),
                     platform_candidate_count=len(direct_ids), public_index_candidate_count=len(public_ids), imported_candidate_count=len(imported_ids),
                     source=f"平台入口采集 {len(direct_ids)} 条；公开网页索引补充 {len(public_ids)} 条（非实时指标）"
                            + (f"；其他导入参考 {len(imported_ids)} 条" if imported_ids else ""))
    result = {
        "schema_version": 1, "title": args.title, "status": "partial", "target_total": args.total,
        "selection_status": selected["status"], "quotas": selected["stats"]["quotas"],
        "samples": selected["samples"], "selection_stats": selected["stats"],
        "collection_status": status, "sources": sources,
        "selection_config": {"days": args.days, "min_likes": {"douyin": args.min_douyin_likes, "xiaohongshu": args.min_xiaohongshu_likes},
                             "max_per_author": args.max_per_author, "heat_scope": "retrievable sample pool; not an official platform-wide ranking"},
        "cross_platform_duplicate_candidates": selected.get("cross_platform_duplicate_candidates", []),
    }
    if any(row.get("source_type") == "public_web_search" for row in result["samples"]):
        result["method_note"] = "部分条目来自公开网页索引。互动数是索引页面显示的历史快照，不代表当前实时数据；仅有标题、正文或机器摘要时，分析标记为初步，未声称观看完整视频。"
    request = {
        "instructions": "阅读每条真实内容证据后填写分析。metadata 只能支持初步分析；最终完成须有实际字幕或画面证据。不能补造指标、画面、评论或因果关系。",
        "schema": {
            "sample_id": "platform:id", "evidence_level": "metadata|subtitle|visual",
            "content_summary": "依据可观察证据的概述", "observed_evidence": [{"evidence_id": "样本 content_evidence 中已存在的 ID"}],
            "hypotheses": [{"claim": "可能的传播因素", "evidence_ids": ["E1"]}],
            "confidence": "low|medium|high", "transferable_idea": "具体可拍的借鉴角度", "limitations": ["未取得的信息"],
        },
        "samples": result["samples"], "analyses": [],
    }
    return _finish(result, output, args.analyses, pending_outputs={
        "candidates.json": {"samples": rows, "sources": sources},
        "selection.json": selected, "analysis_request.json": request, "collection_status.json": status,
    })


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command == "doctor":
            print(json.dumps({"title_tag_source_exists": (TITLE_TAG / "src").is_dir(), "python": sys.executable,
                              "playwright_installed": importlib.util.find_spec("playwright") is not None,
                              "ffmpeg": shutil.which("ffmpeg"), "mode": "当前助手审核；不要求模型 API"}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "login":
            login(args.platform)
            return 0
        output = args.output.expanduser().resolve()
        if args.command in {"prepare", "run"}:
            _preflight(args, output)
        output.mkdir(parents=True, exist_ok=True)
        if args.command == "collect":
            status = _collect(args, output)
            return 0 if all(v.get("status") == "ok" for v in status.values()) else 2
        if args.command == "render":
            result = _finish(read_payload(output / "report_data.json"), output, args.analyses)
        else:
            if args.command == "run" and not args.skip_collect:
                _collect(args, output)
            result = _prepare(args, output)
        print(f"状态：{result['status']}；入选 {len(result['samples'])}/{result['target_total']}；HTML：{output / 'report.html'}")
        return 0 if result["status"] == "complete" else 2
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, ImportError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
