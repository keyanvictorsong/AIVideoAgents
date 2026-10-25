"""Private recovery of an already verified creator session.

Chrome's persistent profile remains authoritative. This backup only fills gaps
after a restart (notably session cookies/sessionStorage); it never extends an
expiry, replaces current browser values, or establishes that a login is valid.
Call ``save_authenticated_session`` only after verifying the creator upload UI.
The snapshot stays inside the dedicated profile and must not enter job output.
"""
from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SESSION_FILENAME = ".xhs-auth-session.json"
MAX_BACKUP_AGE_SECONDS = 7 * 24 * 60 * 60
_DOMAINS = ("xiaohongshu.com", "rednote.com")
_CREATOR_HOSTS = ("creator.xiaohongshu.com", "creator.rednote.com")
_COOKIE_FIELDS = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "partitionKey"}


class SessionStateError(RuntimeError):
    """Messages deliberately contain no browser state or credential values."""


def _allowed_host(host: str) -> bool:
    host = host.lower().lstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in _DOMAINS)


def _origin(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or not _allowed_host(parsed.hostname)
                or parsed.port not in (None, 443) or parsed.username or parsed.password):
            return None
        return "https://" + parsed.hostname.lower()
    except (TypeError, ValueError):
        return None


def _cookie_key(cookie: dict) -> tuple:
    return (cookie.get("name"), cookie.get("domain", "").lower().lstrip("."),
            cookie.get("path", "/"), cookie.get("partitionKey"))


def _entries(entries: Any) -> list[dict]:
    if not isinstance(entries, list):
        return []
    return [{"name": item["name"], "value": item["value"]} for item in entries
            if isinstance(item, dict) and isinstance(item.get("name"), str)
            and isinstance(item.get("value"), str)]


def _profile_file(profile_dir: str | Path) -> Path:
    profile = Path(profile_dir).expanduser().resolve()
    if not profile.is_dir():
        raise SessionStateError("专用浏览器资料目录不存在。")
    return profile / SESSION_FILENAME


def _atomic_private_json(path: Path, data: dict) -> None:
    """No temporary or final credential file is ever world/group readable."""
    temp_path: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".xhs-session-", dir=path.parent)
        temp_path = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, TypeError, ValueError):
        raise SessionStateError("私有登录会话未能安全保存。") from None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _read_private_snapshot(path: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise SessionStateError("私有登录会话文件无法安全读取。") from None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 8 * 1024 * 1024:
                raise SessionStateError("私有登录会话文件权限或格式不符合要求。")
            data = json.load(handle)
        if not isinstance(data, dict) or data.get("version") != 1:
            raise SessionStateError("私有登录会话格式不受支持。")
        return data
    except (OSError, ValueError, UnicodeError):
        raise SessionStateError("私有登录会话文件已损坏。") from None


@dataclass
class SessionRestore:
    """Safe counts for reporting; the CDP handle/script must never be exported."""
    status: str
    cookies_restored: int = 0
    storage_origins: int = 0
    _cdp: Any = field(default=None, repr=False)
    _script_id: str | None = field(default=None, repr=False)

    def summary(self) -> dict:
        return {"status": self.status, "cookies_restored": self.cookies_restored,
                "storage_origins": self.storage_origins}

    def stop(self) -> None:
        """Stop recovery injection before saving, handoff, logout or an auth error."""
        cdp, script_id = self._cdp, self._script_id
        self._cdp, self._script_id = None, None
        if cdp is not None:
            try:
                if script_id:
                    cdp.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": script_id})
            except Exception:
                # A tab closed by its user has no future documents to inject.
                # Detaching also releases this session's CDP registration.
                pass
            finally:
                try:
                    cdp.detach()
                except Exception:
                    pass


def restore_saved_session(context: Any, profile_dir: str | Path, page: Any) -> SessionRestore:
    """Call on a fresh dedicated context/page before navigating to the creator.

    This Chrome-only, removable script avoids leaving a permanent initializer
    that could resurrect storage after a later logout. Call result.stop() after
    checking the upload/login UI; save_authenticated_session also stops it.
    """
    snapshot = _read_private_snapshot(_profile_file(profile_dir))
    if snapshot is None:
        return SessionRestore("no_saved_session")
    timestamp = snapshot.get("captured_at")
    current_time = time.time()
    if (not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp > current_time + 300
            or current_time - timestamp > MAX_BACKUP_AGE_SECONDS):
        return SessionRestore("saved_session_too_old")
    existing = {_cookie_key(cookie) for cookie in context.cookies()}
    missing = []
    for cookie in snapshot.get("cookies", []):
        if not isinstance(cookie, dict) or not _allowed_host(cookie.get("domain", "")):
            continue
        expiry = cookie.get("expires", -1)
        if not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or (expiry != -1 and expiry <= current_time):
            continue
        if _cookie_key(cookie) in existing:
            continue
        if not all(isinstance(cookie.get(key), str) for key in ("name", "value", "domain", "path")):
            continue
        missing.append({key: value for key, value in cookie.items() if key in _COOKIE_FIELDS})
        existing.add(_cookie_key(cookie))
    if missing:
        try:
            context.add_cookies(missing)
        except Exception:
            # A protocol error must not echo cookie arguments into logs.
            raise SessionStateError("私有登录 Cookie 未能恢复。") from None
    origins = {}
    for item in snapshot.get("origins", []):
        if isinstance(item, dict) and _origin(item.get("origin", "")) == item.get("origin"):
            origins[item["origin"]] = {"localStorage": _entries(item.get("localStorage", [])),
                                      "sessionStorage": _entries(item.get("sessionStorage", []))}
    result = SessionRestore("restoration_installed", len(missing), len(origins))
    if origins:
        # Never log this script: it contains credentials by design. The exact
        # HTTPS origin guard prevents cross-origin disclosure, including frames.
        script = """(() => {
            const states = %s, state = states[location.origin];
            if (!state) return;
            for (const kind of ['localStorage', 'sessionStorage']) {
                for (const item of state[kind]) {
                    try { if (window[kind].getItem(item.name) === null)
                        window[kind].setItem(item.name, item.value); } catch (_) {}
                }
            }
        })();""" % json.dumps(origins, ensure_ascii=True)
        cdp = context.new_cdp_session(page)
        try:
            cdp.send("Page.enable")
            script_id = cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": script})["identifier"]
        except Exception:
            cdp.detach()
            raise SessionStateError("私有登录会话恢复脚本无法安装。") from None
        result._cdp, result._script_id = cdp, script_id
    return result


def save_authenticated_session(context: Any, profile_dir: str | Path, verified_page: Any,
                               restoration: SessionRestore | None = None) -> dict:
    """Save only after caller verified the signed-in creator upload controls.

    The verified page takes precedence when multiple tabs of the same origin
    have different sessionStorage. Other origins remain isolated in the file.
    The return value contains counts only and is safe for a job report.
    """
    if restoration is not None:
        restoration.stop()
    verified_origin = _origin(verified_page.url)
    if verified_origin is None or urlsplit(verified_origin).hostname not in _CREATOR_HOSTS:
        raise SessionStateError("只能保存已核对的小红书创作者后台会话。")
    state = context.storage_state()
    cookies = [{key: value for key, value in cookie.items() if key in _COOKIE_FIELDS}
               for cookie in state.get("cookies", []) if _allowed_host(cookie.get("domain", ""))]
    origins = {}
    for item in state.get("origins", []):
        origin = _origin(item.get("origin", ""))
        if origin and origin == item.get("origin"):
            origins[origin] = {"origin": origin, "localStorage": _entries(item.get("localStorage", [])), "sessionStorage": []}
    seen = set()
    for page in [verified_page, *context.pages]:
        origin = _origin(page.url)
        if origin is None or origin in seen:
            continue
        seen.add(origin)
        try:
            values = page.evaluate("""() => ({origin:location.origin,
                entries:Object.entries(sessionStorage).map(([name,value])=>({name,value}))})""")
            if values.get("origin") != origin:
                continue
            origins.setdefault(origin, {"origin": origin, "localStorage": [], "sessionStorage": []})["sessionStorage"] = _entries(values.get("entries", []))
        except Exception:
            # A tab that navigates/closes during capture must not contaminate
            # another origin or prevent saving the verified tab's cookies.
            continue
    snapshot = {"version": 1, "captured_at": time.time(), "verified_origin": verified_origin,
                "cookies": cookies, "origins": list(origins.values())}
    _atomic_private_json(_profile_file(profile_dir), snapshot)
    return {"status": "verified_session_saved", "cookies_saved": len(cookies), "storage_origins": len(origins)}


def invalidate_saved_session(profile_dir: str | Path, restoration: SessionRestore | None = None) -> dict:
    """Discard only our backup after confirmed logout/auth rejection.

    Chrome's current cookies/profile remain untouched; no expiry is rewritten.
    """
    if restoration is not None:
        restoration.stop()
    path = _profile_file(profile_dir)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        raise SessionStateError("无法移除已失效的私有会话备份。") from None
    return {"status": "saved_session_invalidated"}
