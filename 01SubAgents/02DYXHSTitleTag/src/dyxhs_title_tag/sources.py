"""Read-only reference collection, with explicit evidence and failure states.

The browser adapter uses its own persistent profiles and observes normal page
responses. It neither reads personal browser cookies nor signs private API calls.
The optional xiaohongshu-mcp REST adapter follows the upstream routes.go,
handlers_api.go and xiaohongshu/search.go (checked 2026-10-18).
"""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

PLATFORMS = {"douyin", "xiaohongshu"}
SHANGHAI = ZoneInfo("Asia/Shanghai")
METRICS = ("likes", "comments", "shares", "collects", "views")
_METRIC_KEYS = {
    "likes": ("likes", "likedCount", "liked_count", "digg_count", "点赞数"),
    "comments": ("comments", "commentCount", "comment_count", "评论数"),
    "shares": ("shares", "sharedCount", "share_count", "分享数"),
    "collects": ("collects", "favorites", "collectedCount", "collected_count", "collect_count", "收藏数"),
    "views": ("views", "play_count", "view_count", "播放量"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_count(value: Any) -> int | None:
    """Normalize displayed counts while keeping absent data unknown."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value)) if math.isfinite(value) and value >= 0 else None
    text = str(value).strip().replace(",", "").replace("，", "")
    if text in {"", "-", "--", "点赞", "收藏", "评论", "分享"}:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([千万亿kKmMwW]?)\+?", text)
    if not match:
        return None
    scale = {"": 1, "千": 1_000, "万": 10_000, "亿": 100_000_000,
             "k": 1_000, "m": 1_000_000, "w": 10_000}
    return int(float(match.group(1)) * scale[match.group(2).lower()])


def parse_published_at(value: Any, *, observed_at: str | None = None) -> str | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"\d{10,13}", str(value)):
            stamp = float(value)
            if stamp > 1e12:
                stamp /= 1000
            return datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        text = str(value).strip()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    # Relative page labels are anchored to the actual observation, never the
    # process clock when importing old exports.
    if observed_at:
        try:
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            match = re.fullmatch(r"(\d+)\s*(分钟|小时|天)前", str(value).strip())
            if match:
                seconds = int(match.group(1)) * {"分钟": 60, "小时": 3600, "天": 86400}[match.group(2)]
                return (observed - timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat()
            if str(value).strip() == "昨天":
                return (observed - timedelta(days=1)).astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return None


def extract_tags(text: str) -> list[str]:
    # XHS exports mark topics as #篮球[话题]#; ordinary captions use #篮球.
    tags = re.findall(r"#([^#\s\[\]，,。！？!?：:；;]+)(?:\[话题\])?", text or "")
    return list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))


@dataclass
class ReferenceSample:
    platform: str
    id: str
    url: str
    title: str = ""
    description: str = ""
    caption: str = ""
    tags: list[str] = field(default_factory=list)
    published_at: str | None = None
    metrics: dict[str, int | None] = field(default_factory=lambda: dict.fromkeys(METRICS))
    metrics_raw: dict[str, Any] = field(default_factory=dict)
    author_id: str | None = None
    source_query: str = ""
    source_sort: str = "unverified"
    source_window_days: int | None = None
    collected_at: str | None = field(default_factory=_now)
    source_type: str = "import"
    content_type: str = "unknown"
    title_derived: bool = False
    published_at_raw: Any = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_recent_video(self, days: int, *, now: datetime | None = None) -> bool:
        if self.content_type != "video" or not self.url or not self.published_at:
            return False
        try:
            published = datetime.fromisoformat(self.published_at)
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            if published.tzinfo is None:
                return False
            return current - timedelta(days=days) <= published <= current
        except (ValueError, TypeError):
            return False


@dataclass
class CollectionResult:
    platform: str
    samples: list[ReferenceSample] = field(default_factory=list)
    status: str = "insufficient_samples"
    reason: str = ""
    target_count: int = 60
    days: int = 7
    attempts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    collected_at: str = field(default_factory=_now)

    @property
    def valid_count(self) -> int:
        now = datetime.fromisoformat(self.collected_at)
        return sum(sample.is_recent_video(self.days, now=now) for sample in self.samples)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(sample_count=len(self.samples), valid_count=self.valid_count,
                    qualification="dated videos in requested window; topical relevance is checked downstream",
                    heat_scope="retrievable sample; not an official platform-wide top list")
        return data


def _first(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return default


def _platform(value: Any, url: str = "") -> str:
    aliases = {"xhs": "xiaohongshu", "小红书": "xiaohongshu", "dy": "douyin", "抖音": "douyin"}
    text = aliases.get(str(value).lower(), str(value).lower())
    if text in PLATFORMS:
        return text
    if "xiaohongshu.com" in url or "xhslink.com" in url:
        return "xiaohongshu"
    if "douyin.com" in url:
        return "douyin"
    raise ValueError("每条参考数据需要 platform=douyin/xiaohongshu 或可识别的平台 URL")


def normalize_reference(raw: dict[str, Any], platform: str | None = None, *,
                        query: str = "", source_type: str = "import",
                        collected_at: str | None = None, days: int | None = None,
                        source_sort: str = "unverified") -> ReferenceSample | None:
    """Normalize canonical, MediaCrawler, Douyin page and XHS MCP rows."""
    if not isinstance(raw, dict):
        return None
    original = raw
    raw = raw.get("aweme_info", raw)
    if not isinstance(raw, dict):
        return None
    card = raw.get("noteCard") or raw.get("note_card") or raw.get("note") or raw
    if not isinstance(card, dict):
        return None
    url = str(_first(raw, "url", "note_url", "aweme_url", "share_url", default=""))
    selected_platform = _platform(platform or raw.get("platform"), url)
    item_id = str(_first(raw, "id", "aweme_id", "note_id", "noteId", default="") or
                  _first(card, "id", "noteId", "note_id", default=""))
    if not item_id and url:
        match = re.search(r"/(?:video|explore|discovery/item)/(\w+)", url)
        item_id = match.group(1) if match else ""
    if not item_id:
        return None
    # Strip per-session xsec/signature parameters from exported evidence links.
    if selected_platform == "xiaohongshu":
        url = f"https://www.xiaohongshu.com/explore/{item_id}"
    elif not url or not urlsplit(url).hostname or "douyin.com" not in (urlsplit(url).hostname or ""):
        url = f"https://www.douyin.com/video/{item_id}"
    else:
        url = url.split("?", 1)[0]
    title = str(_first(card, "title", "displayTitle", "display_title", default="") or "")
    description = str(_first(card, "description", "desc", default="") or "")
    caption = str(_first(card, "caption", "desc", "description", default="") or "")
    if selected_platform == "douyin" and not caption:
        caption = title
    # A Douyin desc is a caption, not a separate platform title.
    title_derived = bool(card.get("title_derived", False))
    user = card.get("user") or card.get("author") or {}
    author_id = _first(card, "author_id", "user_id", default=None)
    if isinstance(user, dict):
        author_id = _first(user, "userId", "user_id", "uid", "sec_uid", default=author_id)
    observed = raw.get("collected_at") or collected_at
    if observed is None and source_type != "import":
        observed = _now()
    published_raw = _first(card, "published_at", "create_time", "time", "publish_time", "published_at_raw")
    statistics = card.get("metrics") or card.get("statistics") or card.get("interactInfo") or card.get("interact_info") or card
    statistics = statistics if isinstance(statistics, dict) else {}
    metrics_raw = {key: _first(statistics, *aliases) for key, aliases in _METRIC_KEYS.items()}
    if isinstance(card.get("metrics_raw"), dict):
        metrics_raw.update(card["metrics_raw"])
    metrics = {key: parse_count(_first(statistics, *aliases)) for key, aliases in _METRIC_KEYS.items()}
    raw_tags = card.get("tags") or card.get("tag_list") or card.get("tagList") or []
    if isinstance(raw_tags, str):
        try:
            decoded = json.loads(raw_tags)
            raw_tags = decoded if isinstance(decoded, list) else raw_tags
        except ValueError:
            pass
        if isinstance(raw_tags, str):
            raw_tags = re.split(r"[,，;；\s]+", raw_tags)
    tags = []
    for tag in raw_tags:
        tag = tag.get("name", "") if isinstance(tag, dict) else str(tag)
        clean = re.sub(r"\[话题\]", "", tag).strip().strip("#")
        if clean:
            tags.append(clean)
    tags.extend(extract_tags(f"{title} {description} {caption}"))
    content_type = str(card.get("content_type") or card.get("type") or "unknown")
    if selected_platform == "douyin" and content_type == "unknown" and not card.get("images") and card.get("aweme_type") not in {2, 68, 150}:
        content_type = "video"
    elif content_type in {"normal", "image", "图文"}:
        content_type = "image"
    elif content_type in {"video", "视频"} or card.get("video"):
        content_type = "video"
    return ReferenceSample(
        platform=selected_platform, id=item_id, url=url, title=title, description=description,
        caption=caption, tags=list(dict.fromkeys(tags)),
        published_at=parse_published_at(published_raw, observed_at=observed),
        published_at_raw=published_raw, metrics=metrics, metrics_raw=metrics_raw,
        author_id=str(author_id) if author_id is not None else None,
        source_query=str(raw.get("source_query") or query),
        source_sort=str(raw.get("source_sort") or source_sort),
        source_window_days=raw.get("source_window_days", days), collected_at=observed,
        source_type=str(original.get("source_type") or source_type), content_type=content_type,
        title_derived=title_derived,
    )


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("samples", "references", "feeds", "aweme_list", "items", "notes"):
        if isinstance(payload.get(key), list):
            return _rows(payload[key])
    if isinstance(payload.get("data"), (list, dict)):
        return _rows(payload["data"])
    if isinstance(payload.get("note"), dict):
        return [payload["note"]]
    if any(key in payload for key in ("aweme_id", "note_id", "noteId", "id", "aweme_info")):
        return [payload]
    return []


def deduplicate_references(samples: Iterable[ReferenceSample]) -> list[ReferenceSample]:
    merged: dict[tuple[str, str], ReferenceSample] = {}
    for sample in samples:
        key = sample.platform, sample.id
        old = merged.get(key)
        if old:
            # Detail data is richer than cards. Never replace known values by
            # missing fields from a subsequent search result.
            for name in ("title", "description", "caption", "published_at", "published_at_raw", "author_id"):
                if not getattr(sample, name):
                    setattr(sample, name, getattr(old, name))
            sample.tags = list(dict.fromkeys(old.tags + sample.tags))
            sample.metrics = {metric: sample.metrics.get(metric) if sample.metrics.get(metric) is not None else old.metrics.get(metric) for metric in METRICS}
            sample.metrics_raw = {metric: sample.metrics_raw.get(metric) if sample.metrics_raw.get(metric) is not None else old.metrics_raw.get(metric) for metric in METRICS}
            if sample.content_type == "unknown":
                sample.content_type = old.content_type
        merged[key] = sample
    return list(merged.values())


def import_references(path: str | Path, platform: str | None = None) -> list[ReferenceSample]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        with path.open(encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        rows = _rows(payload)
    references = []
    for row in rows:
        sample = normalize_reference(row, platform=platform, source_type="import")
        if sample and (platform is None or sample.platform == platform):
            references.append(sample)
    return deduplicate_references(references)


def _write_result(result: CollectionResult, output_dir: str | Path | None) -> None:
    if output_dir:
        path = Path(output_dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{result.platform}.json").write_text(json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _http_json(base: str, path: str, body: dict[str, Any] | None = None, *, timeout: int = 25) -> Any:
    url = base.rstrip("/").removesuffix("/mcp") + path
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def _mcp_collect(result: CollectionResult, queries: list[str], base: str) -> bool:
    """Use only upstream read endpoints. True means stop browser fallback."""
    try:
        payload = _http_json(base, "/api/v1/login/status")
    except HTTPError as exc:
        result.attempts.append({"adapter": "xiaohongshu_mcp", "status": "authentication_required" if exc.code == 401 else "service_error", "http_status": exc.code})
        if exc.code == 401:
            result.status, result.reason = "authentication_required", "小红书 MCP 服务要求鉴权；请通过已授权服务或导入文件提供参考数据。"
            return True
        return False
    except (URLError, TimeoutError, ValueError, OSError):
        result.attempts.append({"adapter": "xiaohongshu_mcp", "status": "service_unavailable"})
        return False
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    if not data.get("is_logged_in", data.get("isLoggedIn", False)):
        result.status, result.reason = "login_required", "小红书 MCP 服务尚未登录；需在该服务的专用会话中手动登录。"
        result.attempts.append({"adapter": "xiaohongshu_mcp", "status": "login_required"})
        return True
    details_seen: set[str] = set()
    for query in queries:
        try:
            payload = _http_json(base, "/api/v1/feeds/search", {
                "keyword": query,
                "filters": {"sort_by": "最多点赞", "note_type": "视频",
                            "publish_time": "一周内" if result.days <= 7 else "半年内"}})
            rows = _rows(payload)
            result.attempts.append({"adapter": "xiaohongshu_mcp", "query": query, "rows": len(rows), "sort": "最多点赞", "window_days": result.days})
            for row in rows:
                sample = normalize_reference(row, "xiaohongshu", query=query, source_type="xiaohongshu_mcp", days=result.days, source_sort="最多点赞")
                if not sample:
                    continue
                result.samples.append(sample)
                # Search cards do not carry dates/descriptions. Verify details;
                # xsecToken stays in memory and is never exported to reports.
                token = row.get("xsecToken") or row.get("xsec_token")
                if token and sample.id not in details_seen:
                    details_seen.add(sample.id)
                    detail = _http_json(base, "/api/v1/feeds/detail", {"feed_id": sample.id, "xsec_token": token, "load_all_comments": False})
                    for detail_row in _rows(detail):
                        enriched = normalize_reference(detail_row, "xiaohongshu", query=query, source_type="xiaohongshu_mcp", days=result.days, source_sort="最多点赞")
                        if enriched:
                            result.samples.append(enriched)
                result.samples = deduplicate_references(result.samples)
                if result.valid_count >= result.target_count:
                    return True
        except HTTPError as exc:
            result.attempts.append({"adapter": "xiaohongshu_mcp", "query": query, "http_status": exc.code})
            if exc.code == 429:
                result.status, result.reason = "rate_limited", "小红书 MCP 返回 HTTP 429，停止继续请求。"
                return True
        except (URLError, TimeoutError, ValueError, OSError):
            result.attempts.append({"adapter": "xiaohongshu_mcp", "query": query, "status": "service_error"})
    return True


def _page_block(text: str, url: str) -> tuple[str, str] | None:
    if re.search(r"验证码|安全验证|验证后继续|完成验证|访问过于频繁|操作频繁|异常访问|captcha|verifycenter", text + " " + url, re.I):
        return "rate_limited", "平台显示验证或访问限制；已停止采集，未尝试自动通过验证。"
    if re.search(r"登录后(?:即可|查看|搜索)|登录以(?:查看|搜索)|请先登录|扫码登录|登录后探索|登录后继续", text):
        return "login_required", "平台搜索需要登录；请在本工具的独立浏览器会话中手动登录后重试。"
    if re.search(r"页面无法访问|网络连接异常|网络错误|暂时无法访问|ERR_CONNECTION", text):
        return "network_error", "平台页面显示网络错误，未取得可核验参考数据。"
    return None


# These scripts inspect hydrated public content in the page, without evaluation
# of downloaded script strings or access to browser credential stores.
_XHS_STATE = """() => {
 const s=window.__INITIAL_STATE__; if(!s) return [];
 const f=s.search?.feeds; let rows=f?.value ?? f?._value ?? [];
 const d=s.note?.noteDetailMap; if(d) for(const v of Object.values(d)) if(v?.note) rows=[...rows,v.note];
 return JSON.parse(JSON.stringify(rows));
}"""
_LINK_ROWS = """platform => [...document.querySelectorAll(platform==='douyin'?'a[href*="/video/"]':'a[href*="/explore/"]')].map(a=>({
 url:a.href, title:(a.innerText||a.getAttribute('title')||'').trim(),
 content_type:platform==='douyin'?'video':'unknown'
})).filter(x=>x.title)"""


def _browser_collect(result: CollectionResult, queries: list[str], *, profile_dir: Path,
                     headless: bool, max_pages: int, output_dir: str | Path | None) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result.status, result.reason = "dependency_missing", "未安装可选采集依赖；运行 pip install -e '.[browser]' 和 python -m playwright install chromium，或使用 JSON/CSV 导入。"
        return
    captured: list[tuple[str, Any]] = []
    active_query = ""
    def observe(response: Any) -> None:
        url = response.url
        if any(key in url for key in ("/aweme/v1/web/general/search", "/aweme/v1/web/search/item", "/api/sns/web/v1/search/notes", "/api/sns/web/v1/feed")):
            try:
                if response.status == 429:
                    captured.append((active_query, {"_rate_limited": True}))
                elif response.status == 200 and "json" in response.headers.get("content-type", ""):
                    captured.append((active_query, response.json()))
            except Exception:
                pass
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            # Use bundled Chromium, or the installed Chrome executable, each
            # with this tool's separate profile (never a personal profile).
            options: dict[str, Any] = {"headless": headless, "viewport": {"width": 1365, "height": 900}, "locale": "zh-CN"}
            try:
                context = playwright.chromium.launch_persistent_context(str(profile_dir), **options)
            except Exception:
                context = playwright.chromium.launch_persistent_context(str(profile_dir), channel="chrome", **options)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.on("response", observe)
                for query in queries:
                    active_query = query
                    url = (f"https://www.douyin.com/search/{quote(query)}?type=video" if result.platform == "douyin"
                           else "https://www.xiaohongshu.com/search_result?" + urlencode({"keyword": query, "source": "web_explore_feed", "type": 51}))
                    attempt: dict[str, Any] = {"adapter": "browser", "query": query, "url": url,
                                              "sort": "unverified", "requested_window_days": result.days}
                    result.attempts.append(attempt)
                    try:
                        response = page.goto(url, wait_until="domcontentloaded", timeout=25000)
                        attempt["http_status"] = response.status if response else None
                        page.wait_for_timeout(2500)
                        previous_count = -1
                        for scroll in range(max_pages):
                            # Verification interstitials can have an empty body
                            # while the document title says 验证码中间页.
                            text = page.title() + "\n" + page.locator("body").inner_text(timeout=5000)
                            if result.platform == "xiaohongshu":
                                try:
                                    for row in page.evaluate(_XHS_STATE):
                                        sample = normalize_reference(row, result.platform, query=query, source_type="browser", days=result.days)
                                        if sample:
                                            result.samples.append(sample)
                                except Exception:
                                    pass
                            for row in page.evaluate(_LINK_ROWS, result.platform):
                                sample = normalize_reference(row, result.platform, query=query, source_type="browser", days=result.days)
                                if sample:
                                    result.samples.append(sample)
                            for captured_query, payload in captured:
                                if isinstance(payload, dict) and payload.get("_rate_limited"):
                                    result.status, result.reason = "rate_limited", "页面搜索请求返回 HTTP 429，停止继续请求。"
                                    return
                                for row in _rows(payload):
                                    sample = normalize_reference(row, result.platform, query=captured_query, source_type="browser_response", days=result.days)
                                    if sample:
                                        result.samples.append(sample)
                            captured.clear()
                            result.samples = deduplicate_references(result.samples)
                            attempt.update(scroll_batches=scroll + 1, sample_count=len(result.samples))
                            block = _page_block(text, page.url)
                            if block:
                                result.status, result.reason = block
                                attempt["status"] = result.status
                                if output_dir:
                                    snapshot = Path(output_dir) / f"{result.platform}_blocked.png"
                                    page.screenshot(path=str(snapshot), full_page=False, timeout=10000)
                                    attempt["screenshot"] = str(snapshot)
                                return
                            if result.valid_count >= result.target_count:
                                return
                            if len(result.samples) == previous_count:
                                break
                            previous_count = len(result.samples)
                            page.mouse.wheel(0, 900)
                            page.wait_for_timeout(1500)
                        attempt["status"] = "collected" if result.samples else "no_results"
                        # No hidden date assumptions: cards without timestamp
                        # remain unqualified even if a page claims 'recent'.
                    except Exception as exc:
                        attempt.update(status="page_error", error_type=type(exc).__name__)
                        # A navigation timeout may still show a verification
                        # title, or it may mean the platform is unreachable.
                        try:
                            diagnostic_text = page.title() + "\n" + page.locator("body").inner_text(timeout=2000)
                            block = _page_block(diagnostic_text, page.url)
                            if block:
                                result.status, result.reason = block
                                attempt["status"] = result.status
                                return
                        except Exception:
                            pass
                        if type(exc).__name__ == "TimeoutError":
                            result.status = "network_timeout"
                            result.reason = "平台搜索页面加载超时，尚未取得可核验条目；请检查网络或稍后重试。"
                            return
                if not result.samples and result.status == "insufficient_samples":
                    result.reason = "公开搜索页面未返回可核验条目；可能需要登录或页面接口已变化。"
            finally:
                context.close()
    except Exception as exc:
        result.status, result.reason = "browser_unavailable", f"无法启动独立采集浏览器（{type(exc).__name__}）；可以安装 Chromium 或导入参考 JSON/CSV。"


def collect_references(platform: str, queries: Iterable[str], *, target_count: int = 60,
                       days: int = 7, max_pages: int = 5,
                       profile_dir: str | Path | None = None, headless: bool = True,
                       imports: Iterable[str | Path] = (), mcp_url: str | None = None,
                       output_dir: str | Path | None = None) -> CollectionResult:
    """Collect real samples; no synthetic fallback and no publishing actions.

    ``samples`` includes undated cards for auditability. ``valid_count`` only
    counts dated videos within ``days``; downstream must verify relevance and
    rank actual visible metrics. ``mcp_url`` is an optional upstream XHS server
    base URL (the standard local 18060 service is probed if omitted).
    """
    if platform not in PLATFORMS:
        raise ValueError(f"不支持的平台：{platform}")
    if target_count < 1 or days < 1 or max_pages < 1:
        raise ValueError("target_count、days 和 max_pages 必须大于 0")
    query_list = list(dict.fromkeys(str(q).strip() for q in queries if str(q).strip()))
    result = CollectionResult(platform=platform, target_count=target_count, days=days)
    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
    for path in imports:
        # An explicit platform in a mixed export must be respected.
        rows = import_references(path)
        rows = [row for row in rows if row.platform == platform]
        result.samples.extend(rows)
        result.attempts.append({"adapter": "import", "path": str(Path(path)), "rows": len(rows)})
    result.samples = deduplicate_references(result.samples)
    if result.valid_count < target_count and query_list:
        stop = False
        if platform == "xiaohongshu":
            stop = _mcp_collect(result, query_list, mcp_url or "http://127.0.0.1:18060")
        if not stop:
            chosen_profile = Path(profile_dir) if profile_dir else Path(__file__).resolve().parents[2] / ".local" / "profiles" / platform
            _browser_collect(result, query_list, profile_dir=chosen_profile, headless=headless,
                             max_pages=max_pages, output_dir=output_dir)
    result.samples = deduplicate_references(result.samples)
    if result.valid_count >= target_count:
        result.status, result.reason = "ok", f"取得 {result.valid_count} 条有日期且在最近 {days} 天内的视频；仍需核验主题相关性并按可见互动排序。"
    elif result.status == "insufficient_samples":
        result.reason = result.reason or f"取得 {len(result.samples)} 条真实参考，其中 {result.valid_count} 条是最近 {days} 天内有日期的视频，未达到 {target_count} 条目标。"
    if result.samples and any(not row.published_at for row in result.samples):
        result.warnings.append("缺失发布时间的条目仅作参考，不计入近期有效样本；缺失指标保留 null。")
    result.warnings.append("公开浏览器搜索排序未核验时 source_sort=unverified；不能据此宣称平台最火前 50。")
    _write_result(result, output_dir)
    return result
