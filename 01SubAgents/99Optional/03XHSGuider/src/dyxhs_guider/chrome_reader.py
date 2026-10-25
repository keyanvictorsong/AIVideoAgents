"""Read public XHS content in one explicitly chosen Google Chrome tab.

No tabs are enumerated, no browser profile is opened, and no cookies, storage,
credentials, private APIs, signing code, or media addresses are read. Apple
Events JavaScript must be enabled by the user in Chrome. This module does not
change that setting. ``evaluate`` is an internal escape hatch for controlled
read-only page expressions; callers must never use it to inspect credentials.
The command-line interface exposes only the sanitized public-state reader.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .collectors import XHS_STATE, classify_page_access


class ChromeReadError(RuntimeError):
    """A safe, stable error: raw AppleScript/browser output is never included."""

    def __init__(self, status: str, reason: str):
        self.status, self.reason = status, reason
        super().__init__(reason)


def _literal(value: str) -> str:
    """Quote data as one AppleScript string, independently of any shell."""
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\r', '\\r').replace('\n', '\\n').replace('\t', '\\t') + '"'


def _xhs_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == "https" and parts.hostname in {"xiaohongshu.com", "www.xiaohongshu.com"}
                 and not parts.username and not parts.password and parts.port in {None, 443})
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("Only HTTPS xiaohongshu.com research pages are supported")
    return value


def _public_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _osascript(script: str, *, timeout: float) -> str:
    try:
        result = subprocess.run(["osascript"], input=script, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except FileNotFoundError:
        raise ChromeReadError("unsupported_platform", "当前系统没有 osascript，无法读取 Google Chrome。") from None
    except subprocess.TimeoutExpired:
        raise ChromeReadError("apple_event_timeout", "Chrome Apple Event 超时；未继续读取。") from None
    if result.returncode:
        # Do not propagate stderr: it can contain a page URL or source text.
        error = result.stderr or ""
        if (re.search(r"\(12\)\s*$", error.strip()) or "javascript from apple events" in error.lower()
                or "allow javascript" in error.lower() or "通过 apple 事件" in error.lower()):
            raise ChromeReadError("javascript_disabled", "请在 Google Chrome 菜单「显示 → 开发者」启用「允许 Apple 事件中的 JavaScript」，再重试。")
        if "-1743" in error or "not authorized" in error.lower() or "not permitted" in error.lower():
            raise ChromeReadError("automation_denied", "macOS 未允许当前进程通过 Apple Events 控制 Google Chrome。")
        if "DYXHS_UNEXPECTED_ORIGIN" in error:
            raise ChromeReadError("unexpected_origin", "指定研究标签当前不是小红书页面；已停止读取或导航。")
        if "-1728" in error or "can't get tab" in error.lower() or "can't get window" in error.lower():
            raise ChromeReadError("tab_not_found", "指定研究标签不存在；未查找或读取其他标签。")
        raise ChromeReadError("apple_event_error", "Chrome Apple Event 执行失败；未输出原始浏览器错误。")
    return result.stdout.strip()


# Public state is projected inside the page, before crossing Apple Events.
# Only real DOM links (or the current detail URL) establish evidence URLs.
PUBLIC_STATE = r"""() => {
 const text = value => {
   if (typeof value === 'number') return Number.isFinite(value) ? value : null;
   if (typeof value !== 'string') return null;
   return value.slice(0, 50000)
    .replace(/https?:\/\/[^\s<>"']+/gi, value => {try {const u=new URL(value);return u.origin+u.pathname;} catch {return '[url]';}})
    .replace(/\b(?:xsec_token|xsecToken|x-s|x-t|signature|access_token|refresh_token)\s*[:=]\s*[^\s&#,;"'}]+/gi, '[redacted]');
 };
 const first = (...values) => values.find(v => (typeof v==='string' && v!=='') || (typeof v==='number' && Number.isFinite(v))) ?? null;
 const itemLink = href => {
   try {
     const u=new URL(href,location.origin);
     if(u.protocol!=='https:' || !['xiaohongshu.com','www.xiaohongshu.com'].includes(u.hostname) || u.username || u.password) return null;
     const match=u.pathname.match(/^\/(?:explore|discovery\/item)\/([a-zA-Z0-9_-]+)\/?$/);
     return match ? {id:match[1],url:u.origin+u.pathname} : null;
   } catch {return null;}
 };
 const links=new Map();
 for(const a of document.querySelectorAll('a[href*="/explore/"],a[href*="/discovery/item/"]')) {
   const link=itemLink(a.getAttribute('href'));
   if(link && !links.has(link.id)) links.set(link.id,{...link,title:text(a.getAttribute('title'))});
 }
 const current=itemLink(location.href);
 if(current && !links.has(current.id)) links.set(current.id,{...current,title:null});
 const rows=new Map();
 for(const raw of (__XHS_STATE__)()) {
   const card=raw.noteCard || raw.note_card || raw.note || raw;
   if(!card || typeof card!=='object') continue;
   const id=first(raw.id,raw.noteId,raw.note_id,card.id,card.noteId,card.note_id);
   if(typeof id!=='string' || !/^[a-zA-Z0-9_-]+$/.test(id)) continue;
   const user=card.user || card.author || {};
   const info=card.interactInfo || card.interact_info || card.metrics || {};
   const row={
     id, url:links.get(id)?.url ?? null,
     title:text(first(card.title,card.displayTitle,card.display_title)),
     description:text(first(card.desc,card.description)),
     author_id:text(first(user.userId,user.user_id,user.uid,card.author_id)),
     author_name:text(first(user.nickname,user.nickName,user.nick_name)),
     content_type:text(first(card.type,card.content_type)),
     published_at_raw:text(first(card.time,card.publishTime,card.publish_time,card.published_at)),
     metrics:{likes:text(first(info.likedCount,info.liked_count,info.likes)),
       comments:text(first(info.commentCount,info.comment_count,info.comments)),
       collects:text(first(info.collectedCount,info.collected_count,info.collects)),
       shares:text(first(info.shareCount,info.share_count,info.shares)),
       views:text(first(info.viewCount,info.view_count,info.views))}
   };
   const old=rows.get(id);
   if(old) {
     for(const key of Object.keys(row)) if(key!=='metrics' && row[key]===null) row[key]=old[key];
     for(const key of Object.keys(row.metrics)) if(row.metrics[key]===null) row.metrics[key]=old.metrics[key];
   }
   rows.set(id,row);
 }
 for(const link of links.values()) if(!rows.has(link.id)) rows.set(link.id,{
   id:link.id,url:link.url,title:link.title,description:null,author_id:null,author_name:null,
   content_type:null,published_at_raw:null,metrics:{likes:null,comments:null,collects:null,shares:null,views:null}
 });
 return {_page:{title:text(document.title),text:text(document.body?.innerText ?? ''),
   url:location.origin+location.pathname},rows:[...rows.values()],links:[...links.values()]};
}""".replace("__XHS_STATE__", XHS_STATE)


@dataclass
class ChromeTab:
    """One fixed tab; absent window_id means that ID in the front window only."""

    tab_id: int
    window_id: int | None = None
    timeout_s: float = 10

    def __post_init__(self) -> None:
        for value in (self.tab_id, self.window_id):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
                raise ValueError("Chrome tab/window IDs must be positive integers")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")

    @property
    def _target(self) -> str:
        window = f"window id {_literal(str(self.window_id))}" if self.window_id is not None else "front window"
        return f"tab id {_literal(str(self.tab_id))} of {window}"

    def _script(self, action: str) -> str:
        # Origin checked via the chosen tab's URL before any JavaScript runs.
        return f'''tell application "Google Chrome"
set researchTab to {self._target}
set researchURL to URL of researchTab
if not ((researchURL starts with "https://www.xiaohongshu.com/") or (researchURL starts with "https://xiaohongshu.com/")) then error "DYXHS_UNEXPECTED_ORIGIN"
{action}
end tell'''

    @classmethod
    def create(cls, url: str) -> "ChromeTab":
        """Create only a new XHS research tab; never inspect existing tabs."""
        _xhs_url(url)
        script = f'''tell application "Google Chrome"
if (count of windows) is 0 then
set researchWindow to make new window
set researchTab to active tab of researchWindow
set URL of researchTab to {_literal(url)}
else
set researchWindow to front window
set researchTab to make new tab at end of tabs of researchWindow with properties {{URL:{_literal(url)}}}
end if
return (id of researchWindow as text) & ":" & (id of researchTab as text)
end tell'''
        result = _osascript(script, timeout=10)
        match = re.fullmatch(r"(\d+):(\d+)", result)
        if not match:
            raise ChromeReadError("invalid_response", "Chrome 未返回研究标签 ID；未读取其他标签。")
        return cls(tab_id=int(match[2]), window_id=int(match[1]))

    def evaluate(self, js: str, *, timeout_s: float | None = None) -> Any:
        """Evaluate a controlled, synchronous read-only expression/function.

        Never pass untrusted JavaScript, cookie/storage/credential reads, or API
        requests here. Use read_public_state for data exported to a report.
        """
        if not isinstance(js, str) or not js.strip():
            raise ValueError("A nonempty JavaScript expression is required")
        wrapped = f'''(() => {{
if (!['xiaohongshu.com','www.xiaohongshu.com'].includes(location.hostname)) return JSON.stringify({{error:'unexpected_origin'}});
try {{const expression=({js});const value=typeof expression==='function'?expression():expression;
return JSON.stringify({{value:value===undefined?null:value}});}}
catch {{return JSON.stringify({{error:'javascript_error'}});}}
}})()'''
        raw = _osascript(self._script(f"return execute researchTab javascript {_literal(wrapped)}"),
                         timeout=self.timeout_s if timeout_s is None else timeout_s)
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            raise ChromeReadError("invalid_response", "Chrome 没有返回可读取的 JSON；未输出原始页面内容。") from None
        if not isinstance(payload, dict):
            raise ChromeReadError("invalid_response", "Chrome 返回的结果结构无效。")
        if payload.get("error"):
            status = payload["error"] if payload["error"] in {"unexpected_origin", "javascript_error"} else "javascript_error"
            raise ChromeReadError(status, "研究标签 JavaScript 执行失败或页面已离开小红书。")
        return payload.get("value")

    def navigate(self, url: str) -> dict:
        """Navigate the fixed research tab; caller waits via read_public_state."""
        _xhs_url(url)
        _osascript(self._script(f"set URL of researchTab to {_literal(url)}\nreturn \"navigated\""), timeout=self.timeout_s)
        return {"status": "navigated", "url": _public_url(url)}

    def read_public_state(self, *, timeout_ms: int = 15000, poll_ms: int = 500) -> dict:
        """Wait within a budget for actual links, public state, or a clear gate."""
        if timeout_ms <= 0 or poll_ms <= 0:
            raise ValueError("timeout_ms and poll_ms must be positive")
        start = time.monotonic()
        deadline = start + timeout_ms / 1000
        polls = 0

        def finish(status: str, *, rows=None, links=None, **extra) -> dict:
            return {"status": status, "rows": rows or [], "links": links or [],
                    "waited_ms": max(0, round((time.monotonic() - start) * 1000)),
                    "poll_count": polls, **extra}

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return finish("hydration_timeout", reason="小红书研究标签在等待预算内未出现作品入口、公开状态或明确阻断提示。")
            polls += 1
            try:
                probe = self.evaluate(PUBLIC_STATE, timeout_s=min(self.timeout_s, remaining))
            except ChromeReadError as exc:
                return finish(exc.status, reason=exc.reason)
            if not isinstance(probe, dict):
                return finish("invalid_response", reason="Chrome 公开状态返回结构无效。")
            page = probe.get("_page") or {}
            block = classify_page_access(page.get("text", ""), page.get("url", ""), title=page.get("title", ""))
            if block:
                return finish(block[0], reason=block[1])
            rows, links = probe.get("rows") or [], probe.get("links") or []
            if links or rows:
                return finish("links_present" if links else "state_received", rows=rows, links=links)
            if re.search(r"暂无搜索结果|没有找到相关|未找到相关|搜索结果为空", page.get("text", "")):
                return finish("no_results")
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(poll_ms / 1000, remaining))
