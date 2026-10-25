"""Prepare a Xiaohongshu creator form; the user alone clicks Publish.

Selectors describe observed controls. Ambiguous controls or missing image
evidence stop preparation. No method in this module submits a publication.
"""
from __future__ import annotations

import hashlib
import base64
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .jobs import ATTEMPT_FILENAME, begin_upload, now, write_json
from .cover_validation import compare_cover
from .session import SessionRestore, SessionStateError, restore_saved_session, save_authenticated_session

UPLOAD_URL = "https://creator.xiaohongshu.com/publish/publish"


class BrowserStepError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _visible(locator: Any) -> list[Any]:
    result = []
    for item in locator.all():
        try:
            if item.is_visible():
                result.append(item)
        except Exception:
            pass
    return result


def _unique(locator: Any, label: str, *, visible: bool = True) -> Any:
    items = _visible(locator) if visible else locator.all()
    if len(items) != 1:
        raise BrowserStepError("ambiguous_control", f"{label}需要唯一匹配，实际 {len(items)} 个。")
    return items[0]


def _file_input(scope: Any, kind: str, selector: str | None = None) -> Any:
    if selector:
        return _unique(scope.locator(selector), f"{kind}上传框", visible=False)
    matches = []
    for item in scope.locator('input[type="file"]').all():
        accept = (item.get_attribute("accept") or "").lower()
        pattern = r"video|\.(?:mp4|mov|m4v|webm)" if kind == "video" else r"image|\.(?:png|jpg|jpeg|webp)"
        if re.search(pattern, accept):
            matches.append(item)
    if len(matches) != 1:
        raise BrowserStepError("file_input_not_identified", f"无法唯一识别{kind}文件框，找到 {len(matches)} 个。")
    return matches[0]


def _caption(manifest: dict) -> str:
    description = manifest.get("description", "").strip()
    tags = " ".join("#" + tag for tag in manifest.get("tags", []))
    return "\n\n".join(part for part in [description, tags] if part)


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _creator_url(url: str, *, publish: bool = False) -> bool:
    """Login bundles can contain hidden upload inputs; the route matters too."""
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in {"creator.xiaohongshu.com", "creator.rednote.com"}
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and (not publish or parsed.path == "/publish" or parsed.path.startswith("/publish/")))
    except ValueError:
        return False


def _creator_auth_url(url: str) -> bool:
    return _creator_url(url) and bool(re.match(r"^/(?:login|sso|auth|oauth|passport|callback)(?:/|$)", urlsplit(url).path))


class _Preparer:
    def __init__(self, page: Any, manifest: dict, config: dict | None = None, timeout_seconds: int = 600,
                 restoration: SessionRestore | None = None):
        self.page, self.manifest = page, manifest
        self.config = config or {}
        self.timeout_seconds = timeout_seconds
        self.output_dir = Path(manifest["output_dir"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir = Path(self.config.get("browser_profile") or manifest.get("browser_profile") or self.output_dir / ".profile")
        self.restoration = restoration
        # Legacy callers without an explicit dedicated profile must not export
        # authentication into their ordinary job output directory.
        self._session_backup_enabled = bool(self.config.get("browser_profile") or manifest.get("browser_profile"))
        self.report: dict = {"status":"working", "stage":"starting", "job_id":manifest.get("job_id"),
                             "started_at":now(), "published":False, "checks":{}, "warnings":[], "login_state_saved":False}
        if restoration is not None:
            self.report["session_restoration"] = restoration.summary()

    def save(self) -> None:
        self.report["updated_at"] = now()
        write_json(self.output_dir / "browser-report.json", self.report)

    def screenshot(self, name: str) -> str | None:
        try:
            path = self.output_dir / name
            self.page.screenshot(path=str(path), full_page=True, timeout=15000)
            return str(path)
        except Exception as exc:
            self.report["warnings"].append("截图未保存：" + type(exc).__name__)
            return None

    def control(self, key: str, fallback: Any, label: str) -> Any:
        return _unique(self.page.locator(self.config[key]) if self.config.get(key) else fallback, label)

    def authenticated_video_input(self) -> Any:
        if not _creator_url(self.page.url, publish=True):
            raise BrowserStepError("not_authenticated_upload_route", "尚未到达已登录的创作者视频发布页。")
        return _file_input(self.page, "video", self.config.get("video_input"))

    def save_verified_session(self) -> bool:
        """Only a real creator publish form can refresh the private backup."""
        try:
            self.authenticated_video_input()
        except Exception:
            # The user may already have closed the handed-off tab. Do not
            # navigate, reopen it, or replace the last verified backup then.
            return False
        if self.restoration is not None:
            self.restoration.stop()
        if not self._session_backup_enabled:
            return False
        try:
            summary = save_authenticated_session(self.page.context, self.profile_dir, self.page)
        except Exception as exc:
            self.report["warnings"].append("私有登录会话未保存：" + type(exc).__name__)
            return False
        self.report.update(login_state_saved=True, session_persistence=summary, session_saved_at=now())
        return True

    def wait_for_upload_page(self) -> Any:
        self.report["stage"] = "authentication"
        # A QR login can already have opened a usable form. Preserve that live
        # session instead of navigating again during the authentication handoff.
        try:
            video_input = self.authenticated_video_input()
        except BrowserStepError:
            pass
        else:
            self.save_verified_session()
            return video_input
        # Preserve an in-progress official login/SSO redirect rather than
        # repeatedly navigating back to the upload URL and interrupting it.
        if not _creator_url(self.page.url, publish=True) and not _creator_auth_url(self.page.url):
            self.page.goto(self.config.get("upload_url", UPLOAD_URL), wait_until="domcontentloaded")
        deadline = time.monotonic() + self.timeout_seconds
        grace = max(0.0, float(self.config.get("auth_restore_grace_seconds", 30)))
        prompt_after = time.monotonic() + min(grace, self.timeout_seconds)
        prompted = False
        while time.monotonic() < deadline:
            try:
                video_input = self.authenticated_video_input()
            except BrowserStepError as exc:
                if (_creator_url(self.page.url, publish=True) and "找到 0 个" not in str(exc)
                        and self.page.locator('input[type="file"]').count()):
                    raise
            else:
                self.save_verified_session()
                return video_input
            text = self.page.locator("body").inner_text()
            if not prompted and time.monotonic() >= prompt_after and re.search(r"短信登录|扫码登录|请先登录|安全验证|滑块验证", text):
                print("请在专用 Chrome 窗口完成小红书登录或验证；会话会保存。", flush=True)
                prompted = True
            tabs = _visible(self.page.get_by_text("上传视频", exact=True)) if _creator_url(self.page.url, publish=True) else []
            if len(tabs) == 1:
                tabs[0].click()
            self.page.wait_for_timeout(500)
        raise BrowserStepError("login_or_upload_page_timeout", "未能确认已登录的小红书视频上传页。")

    def upload_video(self) -> None:
        if (self.output_dir / ATTEMPT_FILENAME).exists():
            raise BrowserStepError("duplicate_upload_attempt", "已有上传或交接记录，保留现有页面并先只读核对。")
        video_input = self.wait_for_upload_page()
        self.report["stage"] = "upload"
        marker = self.output_dir / ATTEMPT_FILENAME
        begin_upload(self.manifest, self.profile_dir)
        self.report["upload_attempt"] = str(marker)
        self.save()
        video_input.set_input_files(self.manifest["video"])
        title = self.page.locator(self.config["title"]) if self.config.get("title") else self.page.get_by_placeholder(re.compile("标题"))
        title.first.wait_for(state="visible", timeout=min(self.timeout_seconds * 1000, 180000))

    def fill_metadata(self) -> None:
        self.report["stage"] = "metadata"
        self.report["checks"].pop("title", None)
        self.report["checks"].pop("description", None)
        title = self.control("title", self.page.get_by_placeholder(re.compile("标题")), "标题")
        title.fill(self.manifest["title"])
        if title.input_value() != self.manifest["title"]:
            raise BrowserStepError("title_not_confirmed", "平台中的标题与已选标题不一致，可能超出限制。")
        self.report["checks"]["title"] = True
        editor = self.control("description", self.page.locator('[contenteditable="true"], textarea'), "正文")
        element = editor.element_handle()
        expected = _caption(self.manifest)
        editor.fill(expected)
        editor.press("Escape")
        title.click()
        actual = element.evaluate("el => { if (!el.isConnected) throw Error('editor detached'); return 'value' in el ? el.value : el.innerText; }")
        if _normalized(actual) != _normalized(expected):
            raise BrowserStepError("description_not_confirmed", "平台中的正文或话题文字与准备内容不一致。")
        self.report["checks"]["description"] = True
        self.report["tags"] = self.manifest.get("tags", [])
        self.report["tag_representation"] = "caption_hashtag_text"
        self.save()

    def wait_upload_ready(self) -> None:
        self.report["stage"] = "video_processing"
        self.report["checks"].pop("video_processed", None)
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            text = self.page.locator("body").inner_text()
            if re.search(r"上传失败|转码失败|视频处理失败|格式不支持", text):
                raise BrowserStepError("video_upload_failed", "小红书页面显示视频上传或处理失败。")
            ready = self.page.locator(self.config["upload_ready"]) if self.config.get("upload_ready") else self.page.get_by_text(re.compile(r"^\s*(上传成功|重新上传|更换视频)\s*$"))
            busy = _visible(self.page.get_by_text(re.compile(r"^\s*(?:视频)?(?:上传中|转码中|处理中)(?:\s*\d+(?:\.\d+)?%)?[.。…]*\s*$")))
            if _visible(ready) and not busy:
                self.report["checks"]["video_processed"] = True
                return
            self.page.wait_for_timeout(500)
        raise BrowserStepError("video_processing_timeout", "未能确认视频上传处理完成。")

    def _image_state(self, scope: Any, selector: str) -> dict | None:
        nodes = scope.locator(selector)
        if nodes.count() > 1:
            raise BrowserStepError("ambiguous_cover_preview", "封面预览必须唯一。")
        visible = _visible(nodes)
        if not visible:
            return None
        node = visible[0]
        info = node.evaluate(r"""async el => {
            const background=getComputedStyle(el).backgroundImage;
            const backgroundSource=background && background.match(/^url\(["']?(.*?)["']?\)$/);
            if (!['IMG','CANVAS'].includes(el.tagName) && !backgroundSource) return null;
            if (el.tagName === 'IMG' && (!el.complete || !el.naturalWidth)) return null;
            if (el.tagName === 'CANVAS' && (!el.width || !el.height)) return null;
            let pixel = null;
            try {
                const c=document.createElement('canvas'); c.width=64; c.height=64;
                const ctx=c.getContext('2d'); ctx.drawImage(el,0,0,64,64);
                const digest=await crypto.subtle.digest('SHA-256',ctx.getImageData(0,0,64,64).data);
                pixel=Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');
            } catch(e) {}
            return {kind:backgroundSource ? 'background' : el.tagName.toLowerCase(),
                    source:el.currentSrc || el.src || (backgroundSource ? backgroundSource[1] : null), pixel_sha256:pixel};
        }""")
        if not info:
            return None
        if not info["pixel_sha256"]:
            info["pixel_sha256"] = hashlib.sha256(node.screenshot(timeout=3000, animations="disabled")).hexdigest()
            info["pixel_method"] = "element_screenshot"
        else:
            info["pixel_method"] = "normalized_canvas_rgba"
        if info.get("source") and info["source"].startswith("https://"):
            info["source"] = info["source"].split("?",1)[0]
        return info

    def _preview_png(self, node: Any) -> bytes:
        # Cropper's white selection overlay changes screenshot brightness. Read
        # the underlying artwork, retaining the actual crop-box geometry.
        data_url = node.evaluate(r"""async el => {
            if (!['IMG','CANVAS'].includes(el.tagName)) {
                const style=getComputedStyle(el), box=el.getBoundingClientRect();
                const match=style.backgroundImage.match(/^url\(["']?(.*?)["']?\)$/);
                if (!match || !['cover','contain','100% 100%'].includes(style.backgroundSize)
                    || Math.abs(box.width/box.height/0.75-1)>0.03) return null;
                const abort=new AbortController(), timer=setTimeout(()=>abort.abort(),10000);
                try {
                    const response=await fetch(match[1],{signal:abort.signal});
                    if (!response.ok) return null;
                    const blob=await response.blob();
                    return await new Promise(resolve=>{
                        const reader=new FileReader();reader.onload=()=>resolve(reader.result);
                        reader.onerror=()=>resolve(null);reader.readAsDataURL(blob);
                    });
                } catch(e) { return null; } finally {clearTimeout(timer);}
            }
            try {
                const width=el.naturalWidth || el.width, height=el.naturalHeight || el.height;
                const box=el.getBoundingClientRect(), clip=el.closest('.cropper-view-box');
                let sx=0,sy=0,sw=width,sh=height;
                if (clip) {
                    const crop=clip.getBoundingClientRect();
                    sx=(crop.left-box.left)*width/box.width; sy=(crop.top-box.top)*height/box.height;
                    sw=crop.width*width/box.width; sh=crop.height*height/box.height;
                }
                const c=document.createElement('canvas');
                const scale=Math.min(1,512/Math.max(sw,sh));
                c.width=Math.max(1,Math.round(sw*scale));c.height=Math.max(1,Math.round(sh*scale));
                c.getContext('2d').drawImage(el,sx,sy,sw,sh,0,0,c.width,c.height);
                return c.toDataURL('image/png');
            } catch(e) { return null; }
        }""")
        if data_url:
            return base64.b64decode(data_url.split(",",1)[1])
        self.page.mouse.move(0,0)
        return node.screenshot(timeout=3000, animations="disabled")

    def set_cover(self) -> None:
        self.report["stage"] = "cover"
        self.report["checks"].pop("cover", None)
        required = ["cover_trigger", "cover_dialog", "cover_input", "cover_imported", "cover_confirm", "cover_ready"]
        if any(not self.config.get(key) for key in required):
            raise BrowserStepError("cover_verification_not_configured", "需要配置实际封面入口、上传框、编辑预览及应用预览，不能沿用默认帧。")
        actual_sha = hashlib.sha256(Path(self.manifest["cover"]).read_bytes()).hexdigest()
        expected_sha = self.manifest.get("cover_source", {}).get("sha256", actual_sha)
        if actual_sha != expected_sha:
            raise BrowserStepError("cover_file_changed", "本地封面与清单 SHA 不一致，不能上传已替换的封面。")
        evidence = {"source_file":self.manifest["cover"], "source_sha256":expected_sha}
        self.report["cover_evidence"] = evidence
        before = self._image_state(self.page, self.config["cover_ready"])
        dialog = self.page.locator(self.config["cover_dialog"])
        if not _visible(dialog):
            if self.config.get("cover_hover"):
                _unique(self.page.locator(self.config["cover_hover"]), "封面预览卡").hover()
            self.control("cover_trigger", None, "封面入口").click()
        dialog.wait_for(state="visible", timeout=30000)
        scope = _unique(dialog, "封面编辑器")
        if self.config.get("cover_upload_tab"):
            _unique(scope.locator(self.config["cover_upload_tab"]), "上传封面标签").click()
        imported_before = self._image_state(scope, self.config["cover_imported"])
        scope.locator(self.config["cover_input"]).wait_for(state="attached", timeout=30000)
        image_input = _file_input(scope, "image", self.config["cover_input"])
        input_element = image_input.element_handle()
        # Upload widgets can clear/remount their input in the change handler.
        # Capture the actual File before that handler runs, without altering it.
        input_element.evaluate("""el => {
            el.__xhsCoverProof = new Promise(resolve => {
                el.addEventListener('change', async () => {
                    const f=el.files && el.files.length===1 ? el.files[0] : null;
                    if (!f) { resolve(null); return; }
                    try {
                        const d=await crypto.subtle.digest('SHA-256',await f.arrayBuffer());
                        resolve({name:f.name,sha256:Array.from(new Uint8Array(d),b=>b.toString(16).padStart(2,'0')).join('')});
                    } catch(e) { resolve(null); }
                }, {once:true,capture:true});
            });
        }""")
        input_element.set_input_files(self.manifest["cover"])
        selected = input_element.evaluate("""async el => {
            const proof=await el.__xhsCoverProof; delete el.__xhsCoverProof; return proof;
        }""")
        if not selected or selected["sha256"] != evidence["source_sha256"]:
            raise BrowserStepError("cover_file_not_confirmed", "平台封面输入框中的文件与本地封面不一致。")
        evidence["selected_file"] = selected
        scope.locator(self.config["cover_imported"]).wait_for(state="visible", timeout=30000)
        if self.config.get("cover_ratio"):
            _unique(scope.locator(self.config["cover_ratio"]), "封面裁切比例").click()

        def changed(target: Any, selector: str, old: dict | None) -> dict:
            deadline = time.monotonic() + min(self.timeout_seconds, int(self.config.get("cover_timeout_seconds", 30)))
            while time.monotonic() < deadline:
                current = self._image_state(target, selector)
                if current:
                    image = _unique(target.locator(selector), "封面图像")
                    validation = compare_cover(self.manifest["cover"], self._preview_png(image))
                    evidence["last_artwork_comparison"] = validation
                    if validation["matches"]:
                        current["artwork_comparison"] = validation
                        current["content_changed"] = old is None or current["pixel_sha256"] != old["pixel_sha256"]
                        return current
                self.page.wait_for_timeout(100)
            raise BrowserStepError("cover_not_confirmed", "封面画面未确认更新，按钮可点或窗口关闭不能证明已采用本地图。")

        evidence["imported"] = changed(scope, self.config["cover_imported"], imported_before)
        evidence["imported_screenshot"] = self.screenshot("cover-imported.png")
        confirm = _unique(scope.locator(self.config["cover_confirm"]), "应用封面")
        confirm.wait_for(state="visible", timeout=30000)
        deadline = time.monotonic() + min(60, self.timeout_seconds)
        while not confirm.is_enabled() and time.monotonic() < deadline:
            self.page.wait_for_timeout(100)
        if not confirm.is_enabled():
            raise BrowserStepError("cover_processing_timeout", "封面尚未处理完成。")
        confirm.click()
        scope.wait_for(state="hidden", timeout=30000)
        evidence["applied"] = changed(self.page, self.config["cover_ready"], before)
        evidence["applied_screenshot"] = self.screenshot("cover-applied.png")
        self.report["checks"]["cover"] = True
        self.save()

    def preflight(self) -> None:
        missing = [key for key in ["title","description","video_processed","cover"] if not self.report["checks"].get(key)]
        if missing:
            raise BrowserStepError("preflight_failed", "尚未完成：" + ", ".join(missing))
        title = self.control("title", self.page.get_by_placeholder(re.compile("标题")), "标题")
        editor = self.control("description", self.page.locator('[contenteditable="true"], textarea'), "正文")
        actual = editor.evaluate("el => 'value' in el ? el.value : el.innerText")
        if title.input_value() != self.manifest["title"] or _normalized(actual) != _normalized(_caption(self.manifest)):
            self.report["checks"].pop("title", None)
            self.report["checks"].pop("description", None)
            raise BrowserStepError("metadata_changed", "交接前标题或正文发生变化，需重新核对。")
        if self.config.get("cover_ready"):
            preview = _unique(self.page.locator(self.config["cover_ready"]), "已应用封面")
            validation = compare_cover(self.manifest["cover"], self._preview_png(preview))
            if not validation["matches"]:
                self.report["checks"].pop("cover", None)
                raise BrowserStepError("cover_changed", "交接前封面与定稿本地图不一致。")
        button = self.control("publish_button", self.page.get_by_role("button", name="发布", exact=True), "发布按钮（只检查）")
        if button.evaluate("el => el.tagName === 'XHS-PUBLISH-BTN' && !el.shadowRoot"):
            self.report["final_control"] = "closed_component_visible_for_user_review"
            return
        if not button.is_enabled():
            raise BrowserStepError("publish_not_ready", "平台最终按钮尚不可用，请核对必填设置。")

    def prepare(self) -> dict:
        self.upload_video()
        self.fill_metadata()
        self.wait_upload_ready()
        self.set_cover()
        self.preflight()
        self.report.update(status="awaiting_user_publish",stage="review",published=False,
                           screenshot=self.screenshot("ready-to-publish.png"),final_publish="user_only")
        self.save()
        return self.report


def _launch(playwright: Any, profile: Path, channel: str) -> Any:
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True)
    profile.chmod(0o700)
    context = playwright.chromium.launch_persistent_context(str(profile),channel=channel,headless=False,
                 viewport={"width":1360,"height":1000},locale="zh-CN")
    context.set_default_timeout(15000)
    context.set_default_navigation_timeout(60000)
    return context


def validate_cover_config(config: dict) -> None:
    """Reject missing setup before starting a browser or uploading a video."""
    required = ("cover_trigger", "cover_dialog", "cover_input", "cover_imported", "cover_confirm", "cover_ready")
    if any(not config.get(key) for key in required):
        raise BrowserStepError("cover_verification_not_configured",
                               "首次使用请配置六个封面选择器，参见 selectors.example.json。")


def run_prepare(manifest: dict, profile: Path, *, channel: str = "chrome", timeout_seconds: int = 600,
                selector_config: dict | None = None) -> dict:
    if not sys.stdin.isatty():
        raise BrowserStepError("interactive_handoff_required", "需要交互式终端保留浏览器供用户亲自确认发布。")
    validate_cover_config(selector_config or {})
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        context = _launch(p,profile,channel)
        config = dict(selector_config or {}, browser_profile=str(profile))
        worker = _Preparer(context.pages[0] if context.pages else context.new_page(),manifest,config,timeout_seconds)
        try:
            _restore_worker_session(worker)
            report = worker.prepare()
            print("READY_FOR_USER：视频、文案与封面已就绪。请在浏览器确认并亲自点发布；操作完成后再按终端回车关闭。",flush=True)
            input()
            report["review"] = "user_finished_browser_session_publication_unverified"
            return report
        except Exception as exc:
            worker.report.update(status="preparation_failed",error={"code":getattr(exc,"code","browser_error"),"message":str(exc)},
                                 screenshot=worker.screenshot("browser-error.png"))
            worker.save()
            raise
        finally:
            _close_preparation_context(worker, context)


def _restore_worker_session(worker: _Preparer) -> None:
    try:
        worker.restoration = restore_saved_session(worker.page.context, worker.profile_dir, worker.page)
        worker.report["session_restoration"] = worker.restoration.summary()
    except SessionStateError as exc:
        # Keep the persistent Chrome profile usable even if the separate
        # recovery file is damaged. No credential-bearing exception is logged.
        worker.report["warnings"].append("继续使用专用浏览器资料：" + str(exc))


def _close_preparation_context(worker: _Preparer, context: Any) -> None:
    try:
        worker.save_verified_session()
    finally:
        try:
            if worker.restoration is not None:
                worker.restoration.stop()
        finally:
            context.close()
            worker.report["browser_closed"] = True
            worker.save()


def login(profile: Path, *, channel: str = "chrome", timeout_seconds: int = 600,
          upload_url: str | None = None) -> dict:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        context = _launch(p,profile,channel)
        page = context.pages[0] if context.pages else context.new_page()
        config = {"browser_profile": str(profile)}
        if upload_url:
            config["upload_url"] = upload_url
        worker = _Preparer(page,{"output_dir":str(Path(profile).parent / "login-diagnostics")},config,timeout_seconds)
        try:
            _restore_worker_session(worker)
            worker.wait_for_upload_page()
            return {"status":"authenticated","login_state_saved":worker.report["login_state_saved"],
                    "session_persistence":worker.report.get("session_persistence", {})}
        finally:
            _close_preparation_context(worker, context)
