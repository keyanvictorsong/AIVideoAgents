"""Reuse the existing read-only collectors without modifying their installation."""
from __future__ import annotations

import importlib
import csv
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[2]
TITLE_TAG = ROOT.parents[1] / "02DYXHSTitleTag"
DEFAULT_QUERIES: list[str] = []


def classify_page_access(text: str, url: str, *, title: str | None = None) -> tuple[str, str] | None:
    """Classify an actual gate, not a mention of verification in a video caption.

    The legacy browser supplies ``page.title() + '\\n' + body.inner_text()``;
    accordingly its first line is the title when no explicit title is passed.
    SMS verification fields inside a login dialog establish a login gate.
    Strong anti-bot challenges still take priority over a visible login dialog.
    HTTP 429 remains handled directly by the legacy response observer.
    """
    text = str(text or "")
    title = text.split("\n", 1)[0].strip() if title is None else str(title).strip()
    try:
        parts = urlsplit(str(url or ""))
        host, path = (parts.hostname or "").lower(), unquote(parts.path).lower()
    except ValueError:
        host, path = "", ""
    brand = r"(?:抖音|小红书|douyin|xiaohongshu|rednote)"
    challenge_title = re.fullmatch(
        rf"(?:{brand}\s*[-_|·:]?\s*)?"
        r"(?:验证码(?:中间页|验证页|页面)?|安全验证|人机验证|验证中心|访问验证|"
        r"captcha(?:\s+verification)?|security\s+check|verify\s+you\s+are\s+human)"
        rf"(?:\s*[-_|·:]\s*{brand})?", title, re.I)
    challenge_url = (host.startswith(("captcha.", "verifycenter."))
                     or re.search(r"/(?:captcha|verifycenter|challenge)(?:/|$)", path))
    challenge_body = re.search(
        r"(?:^|\n)\s*(?:请(?:先)?完成)?(?:安全验证|人机验证|滑块验证|滑动验证|完成验证|验证后继续)(?:\s|$|[，,:：。]|后继续)|"
        r"(?:拖动|滑动|按住)[^\n]{0,24}滑块|"
        r"(?:请|需要)(?:先)?(?:(?:完成|进行)(?:安全验证|人机验证|滑块验证|验证)|验证后继续)|"
        r"(?:访问|操作|请求)(?:过于|太)?频繁|频繁(?:访问|操作|请求)|异常访问|检测到异常访问|"
        r"too\s+many\s+requests|requests?\s+(?:are\s+)?too\s+frequent|unusual\s+traffic|"
        r"verify\s+(?:that\s+)?you\s+are\s+human",
        text, re.I)
    if challenge_title or challenge_url or challenge_body:
        return "rate_limited", "平台显示安全验证或访问频率限制；已停止采集，需在专用会话中人工处理。"
    login_phrase = re.search(
        r"登录后(?:即可)?(?:查看|搜索|探索|继续)|登录以(?:查看|搜索)|请先登录|"
        r"(?:^|\n)\s*(?:扫码登录|手机号登录|手机登录|短信登录|账号登录|登录/注册|登录或注册)(?:\s|$)", text)
    phone_field = re.search(r"(?:^|\n)\s*(?:请)?(?:输入)?(?:手机号码|手机号)\s*(?:\n|$)", text)
    sms_field = re.search(r"(?:^|\n)\s*(?:请输入验证码|输入验证码|获取验证码|发送验证码|验证码登录)\s*(?:\n|$)", text)
    login_url = re.search(r"/(?:login|signin)(?:/|$)", path)
    if login_phrase or (phone_field and sms_field) or login_url:
        return "login_required", "平台搜索需要登录；请在专用浏览器会话中手动登录后重试。短信验证码输入框属于登录流程。"
    if re.search(r"页面无法访问|网络连接异常|网络错误|暂时无法访问|ERR_CONNECTION", text, re.I):
        return "network_error", "平台页面显示网络错误，未取得可核验参考数据。"
    return None


# Read already-hydrated public state only. Different site versions expose feeds
# as a plain array, a Vue ref's .value, or its serialized ._value. No requests,
# downloaded-code evaluation, or login bypass are added by this compatibility.
XHS_STATE = """() => {
 const s=window.__INITIAL_STATE__; if(!s) return [];
 const array=v=>Array.isArray(v)?v:Array.isArray(v?.value)?v.value:Array.isArray(v?._value)?v._value:[];
 let rows=[...array(s.search?.feeds)];
 const raw=s.note?.noteDetailMap; const details=raw?.value ?? raw?._value ?? raw;
 if(details && typeof details==='object')
   for(const entry of Object.values(details)) if(entry?.note) rows.push(entry.note);
 return JSON.parse(JSON.stringify(rows.filter(row=>row && typeof row==='object')));
}"""


def _observed_metadata(raw: dict, sample) -> dict:
    """Keep public detail fields that the legacy reference dataclass omits."""
    raw = raw.get("aweme_info", raw)
    if not isinstance(raw, dict):
        return {}
    card = raw.get("noteCard") or raw.get("note_card") or raw
    author = card.get("author") or card.get("user") or {}
    result = {}
    if isinstance(author, dict):
        nickname = author.get("nickname") or author.get("nickName") or author.get("nick_name")
        if nickname:
            result["author_name"] = str(nickname)
    description = card.get("desc") or card.get("description")
    if description:
        result.update(description=str(description), caption=str(description))
        if sample.platform == "douyin":
            result.update(title=str(description), title_derived=True)
    video = card.get("video") or {}
    if isinstance(video, dict):
        urls = []
        for key in ("play_addr", "download_addr", "playAddr", "downloadAddr"):
            address = video.get(key) or {}
            values = address.get("url_list", []) if isinstance(address, dict) else [address] if isinstance(address, str) else []
            urls.extend(value for value in values if isinstance(value, str) and value.startswith(("https://", "http://")))
        if urls:
            result["media_urls"] = list(dict.fromkeys(urls))
        if isinstance(video.get("duration"), (int, float)):
            result["duration_ms"] = video["duration"]
    return result


def _wait_for_hydration(page, platform: str, *, response_probe=None,
                        timeout_ms: int = 15000, poll_ms: int = 500, clock=None) -> dict:
    """Wait once for a search response, actual item links, or an explicit gate.

    Readiness only means candidates can be inspected; it does not establish
    publication dates, interaction metrics, video type, or popular status.
    The 15-second budget starts after navigation, not at browser startup.
    """
    if timeout_ms <= 0 or poll_ms <= 0:
        raise ValueError("timeout_ms and poll_ms must be positive")
    clock = clock or time.monotonic
    start = clock()
    deadline = start + timeout_ms / 1000
    polls, last_error = 0, None
    selector = 'a[href*="/video/"]' if platform == "douyin" else 'a[href*="/explore/"]'

    def finish(status: str, **details) -> dict:
        return {"status": status, "waited_ms": max(0, round((clock() - start) * 1000)), "poll_count": polls, **details}

    while True:
        polls += 1
        response = response_probe() if response_probe else {}
        if response.get("rate_limited"):
            return finish("rate_limited", reason="页面搜索请求返回 HTTP 429，停止继续请求。")
        try:
            title = page.title()
            body = page.locator("body").inner_text(timeout=min(1000, max(1, round((deadline - clock()) * 1000))))
            block = classify_page_access(title + "\n" + body, page.url, title=title)
            if block:
                return finish(block[0], reason=block[1])
            link_count = page.locator(selector).count()
            if link_count:
                return finish("links_present", result_link_count=link_count)
            if response.get("row_count", 0):
                return finish("response_received", response_row_count=response["row_count"])
            if platform == "xiaohongshu":
                state_rows = page.evaluate(XHS_STATE)
                if isinstance(state_rows, list) and state_rows:
                    return finish("state_received", state_row_count=len(state_rows))
            if re.search(r"暂无搜索结果|没有找到相关|未找到相关|搜索结果为空", body):
                return finish("no_results")
        except Exception as exc:
            last_error = type(exc).__name__
        remaining_ms = round((deadline - clock()) * 1000)
        if remaining_ms <= 0:
            return finish("hydration_timeout", **({"last_error_type": last_error} if last_error else {}))
        page.wait_for_timeout(min(poll_ms, remaining_ms))


def _browser_collect(result, queries: list[str], *, source_module, profile_dir: Path,
                     headless: bool, max_pages: int, output_dir: Path) -> None:
    """Guider browser adapter; reuse legacy schemas/parsers, not its polling.

    Reads the same public response/state/link helpers as TitleTag. Initial
    hydration has an explicit budget; scroll batches keep their existing limit.
    No legacy function globals or bytecode are modified.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result.status, result.reason = "dependency_missing", "未安装 Playwright；可使用现有环境或导入真实 JSON/CSV。"
        return
    captured = []
    active_query = ""

    def observe(response) -> None:
        if not any(key in response.url for key in ("/aweme/v1/web/general/search", "/aweme/v1/web/search/item", "/api/sns/web/v1/search/notes", "/api/sns/web/v1/feed")):
            return
        try:
            if response.status == 429:
                captured.append((active_query, {"_rate_limited": True}))
            elif response.status == 200 and "json" in response.headers.get("content-type", ""):
                captured.append((active_query, response.json()))
        except Exception:
            pass

    def response_probe() -> dict:
        pending = [payload for query, payload in captured if query == active_query]
        return {"rate_limited": any(isinstance(payload, dict) and payload.get("_rate_limited") for payload in pending),
                "row_count": sum(len(source_module._rows(payload)) for payload in pending)}

    def save_block(page, attempt) -> None:
        try:
            snapshot = output_dir / f"{result.platform}_blocked.png"
            page.screenshot(path=str(snapshot), full_page=False, timeout=5000)
            attempt["screenshot"] = str(snapshot)
        except Exception as exc:
            attempt["screenshot_error_type"] = type(exc).__name__

    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            options = {"headless": headless, "viewport": {"width": 1365, "height": 900}, "locale": "zh-CN"}
            try:
                context = playwright.chromium.launch_persistent_context(str(profile_dir), **options)
            except Exception:
                context = playwright.chromium.launch_persistent_context(str(profile_dir), channel="chrome", **options)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.on("response", observe)
                for query in queries:
                    active_query = query
                    captured.clear()
                    url = (f"https://www.douyin.com/search/{quote(query)}?type=video" if result.platform == "douyin"
                           else "https://www.xiaohongshu.com/search_result?" + urlencode({"keyword": query, "source": "web_explore_feed", "type": 51}))
                    attempt = {"adapter": "browser", "query": query, "url": url, "sort": "unverified", "requested_window_days": result.days,
                               "hydration_timeout_ms": 15000}
                    result.attempts.append(attempt)
                    try:
                        try:
                            response = page.goto(url, wait_until="domcontentloaded", timeout=25000)
                            attempt["http_status"] = response.status if response else None
                        except Exception as exc:
                            attempt["navigation_error_type"] = type(exc).__name__
                            if type(exc).__name__ != "TimeoutError":
                                raise
                        hydration = _wait_for_hydration(page, result.platform, response_probe=response_probe)
                        attempt["hydration"] = hydration
                        if hydration["status"] in {"rate_limited", "login_required", "network_error"}:
                            result.status, result.reason = hydration["status"], hydration["reason"]
                            attempt["status"] = result.status
                            save_block(page, attempt)
                            return
                        if hydration["status"] == "hydration_timeout":
                            result.status = "hydration_timeout"
                            result.reason = "搜索页面在 15 秒等待预算内未出现结果响应、作品入口或明确登录/验证提示；未将等待结束当成无结果证明。"
                            attempt["status"] = result.status
                            return
                        if hydration["status"] == "no_results":
                            attempt["status"] = "no_results"
                            continue
                        previous_count = -1
                        for scroll in range(max_pages):
                            title = page.title()
                            block = classify_page_access(title + "\n" + page.locator("body").inner_text(timeout=2000), page.url, title=title)
                            if block:
                                result.status, result.reason = block
                                attempt["status"] = result.status
                                save_block(page, attempt)
                                return
                            batches = []
                            if result.platform == "xiaohongshu":
                                batches.append((query, page.evaluate(XHS_STATE), "browser"))
                            batches.append((query, page.evaluate(source_module._LINK_ROWS, result.platform), "browser"))
                            for captured_query, payload in captured:
                                if isinstance(payload, dict) and payload.get("_rate_limited"):
                                    result.status, result.reason = "rate_limited", "页面搜索请求返回 HTTP 429，停止继续请求。"
                                    attempt["status"] = result.status
                                    return
                                batches.append((captured_query, source_module._rows(payload), "browser_response"))
                            captured.clear()
                            for captured_query, rows, source_type in batches:
                                for row in rows:
                                    sample = source_module.normalize_reference(row, result.platform, query=captured_query, source_type=source_type, days=result.days)
                                    if sample:
                                        result.samples.append(sample)
                                        details = _observed_metadata(row, sample)
                                        if details:
                                            result.guider_details.setdefault(sample.id, {}).update(details)
                            result.samples = source_module.deduplicate_references(result.samples)
                            attempt.update(scroll_batches=scroll + 1, sample_count=len(result.samples), dated_video_count=result.valid_count,
                                           samples_with_metrics=sum(any(value is not None for value in row.metrics.values()) for row in result.samples))
                            if result.valid_count >= result.target_count:
                                attempt["status"] = "candidates_collected"
                                return
                            if len(result.samples) == previous_count:
                                break
                            previous_count = len(result.samples)
                            page.mouse.wheel(0, 900)
                            page.wait_for_timeout(1500)
                        attempt["status"] = "candidates_collected" if result.samples else "no_parsed_candidates"
                    except Exception as exc:
                        attempt.update(status="page_error", error_type=type(exc).__name__)
                        result.status, result.reason = "page_error", "搜索页面读取失败；保留已取得候选，未补造日期或互动指标。"
                        return
            finally:
                try:
                    context.close()
                except Exception:
                    result.warnings.append("采集浏览器关闭失败；已保留此前真实采集状态。")
    except Exception as exc:
        result.status, result.reason = "browser_unavailable", f"无法启动独立采集浏览器（{type(exc).__name__}）；可导入真实 JSON/CSV。"


def legacy_module(name: str):
    source = TITLE_TAG / "src"
    if not source.is_dir():
        raise RuntimeError(f"找不到可复用的 TitleTag 源码：{source}")
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    return importlib.import_module(f"dyxhs_title_tag.{name}")


def collect_platform(platform: str, *, output: Path, queries: list[str], target: int = 100,
                     days: int = 90, max_pages: int = 6, headed: bool = False,
                     mcp_url: str | None = None) -> dict[str, Any]:
    if platform not in {"douyin", "xiaohongshu"}:
        raise ValueError("platform must be douyin or xiaohongshu")
    if not queries or not all(isinstance(query, str) and query.strip() for query in queries):
        raise ValueError("queries must contain nonempty strings")
    if any(type(value) is not int or value < 1 for value in (target, days, max_pages)):
        raise ValueError("target, days and max_pages must be positive integers")
    output.mkdir(parents=True, exist_ok=True)
    source_module = legacy_module("sources")
    result = source_module.CollectionResult(platform=platform, target_count=target, days=days)
    result.guider_details = {}
    queries = list(dict.fromkeys(query.strip() for query in queries))
    stop = platform == "xiaohongshu" and source_module._mcp_collect(result, queries, mcp_url or "http://127.0.0.1:18060")
    if not stop:
        _browser_collect(result, queries, source_module=source_module, profile_dir=TITLE_TAG / ".local" / "profiles" / platform,
                         headless=not headed, max_pages=max_pages, output_dir=output)
    result.samples = source_module.deduplicate_references(result.samples)
    if result.valid_count >= target:
        result.status, result.reason = "ok", f"取得 {result.valid_count} 条窗口内有日期的视频候选；热门资格仍须核验主题及互动指标。"
    elif result.status == "insufficient_samples":
        result.reason = result.reason or f"取得 {len(result.samples)} 条候选，其中 {result.valid_count} 条为窗口内有日期的视频；不足 {target} 条目标。"
    if any(not row.published_at for row in result.samples):
        result.warnings.append("仅有作品入口的候选不计入有日期有效视频；缺失日期和互动指标保持未知。")
    result.warnings.append("浏览器排序未核验时 source_sort=unverified；作品入口数量不代表热门样本数或已取得互动指标。")
    payload = result.as_dict()
    for sample in payload["samples"]:
        sample.update(result.guider_details.get(sample["id"], {}))
    temporary = output / f".{platform}.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output / f"{platform}.json")
    return payload


def read_payload(path: Path) -> Any:
    path = path.expanduser()
    if path.suffix.lower() == ".csv":
        # Preserve raw provenance and evidence. The legacy CSV importer drops
        # those fields and may synthesize an item URL before Guider can reject it.
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            for key in ("metrics", "metrics_raw", "content_evidence", "evidence", "source_snapshot"):
                if row.get(key):
                    try:
                        row[key] = json.loads(row[key])
                    except ValueError as exc:
                        raise ValueError(f"CSV 的 {key} 列不是有效 JSON：{path}") from exc
            if row.get("actual_video_watched") in {"true", "false"}:
                row["actual_video_watched"] = row["actual_video_watched"] == "true"
        return {"samples": rows}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def login(platform: str) -> None:
    """User completes login/verification in a dedicated collector profile."""
    if platform not in {"douyin", "xiaohongshu"}:
        raise ValueError("platform must be douyin or xiaohongshu")
    from playwright.sync_api import sync_playwright
    source_module = legacy_module("sources")
    profile = Path(source_module.__file__).resolve().parents[2] / ".local" / "profiles" / platform
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        options = {"headless": False, "locale": "zh-CN"}
        try:
            ctx = p.chromium.launch_persistent_context(str(profile), **options)
        except Exception:
            ctx = p.chromium.launch_persistent_context(str(profile), channel="chrome", **options)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.douyin.com" if platform == "douyin" else "https://www.xiaohongshu.com/explore",
                      wait_until="domcontentloaded", timeout=30000)
            input("在专用窗口内完成登录或验证，完成后回到终端按回车保存会话：")
        finally:
            ctx.close()
