"""Headed Douyin publishing with a dedicated, persistent Playwright profile.

The production page is intentionally treated as an external interface.  If a
control is ambiguous or a required state cannot be confirmed, this adapter stops
and saves a screenshot instead of guessing.  No existing browser cookies are read.

``selector_config`` accepts CSS selectors for ``video_input``, ``title``,
``description``, ``upload_ready``, ``cover_vertical_trigger``,
``cover_horizontal_trigger``, ``cover_dialog``, ``cover_image_input``,
``cover_local_tab``, ``cover_next``, ``cover_confirm``, ``cover_vertical_ready``,
``cover_horizontal_ready``, ``cover_imported``, ``cover_vertical_imported``,
``cover_horizontal_imported``, ``collection_trigger``, ``collection_options``, ``collection_ready``,
``declaration_trigger``, ``declaration_options``, ``publish_button``,
``publish_success``, and ``authenticated``.  Selectors should match one intended
control, except collection/declaration options.  ``review`` (default True) pauses
for Enter on an interactive terminal before closing a filled form.  Optional
``upload_timeout_seconds`` and ``success_timeout_seconds`` override wait budgets.
``cover_evidence_timeout_seconds`` bounds each import/application evidence wait
(default 30 seconds, capped by the task timeout). Import and ready selectors
must identify actual img/canvas previews; cover checks require changed pixels.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME_URL = "https://creator.douyin.com/creator-micro/home"
UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"


class BrowserStepError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _visible(locator: Any) -> list[Any]:
    """Return visible matches; detached elements are harmless during rendering."""
    result = []
    for item in locator.all():
        try:
            if item.is_visible():
                result.append(item)
        except Exception:
            continue
    return result


def _unique(locator: Any, description: str, *, visible: bool = True) -> Any:
    matches = _visible(locator) if visible else locator.all()
    if len(matches) != 1:
        raise BrowserStepError(
            "ambiguous_control",
            f"{description}应有 1 个明确匹配，实际找到 {len(matches)} 个；请调整 selector_config。",
        )
    return matches[0]


def _text(locator: Any) -> str:
    return locator.inner_text(timeout=3000).strip()


def _has_visible(locator: Any) -> bool:
    return bool(_visible(locator))


def _file_input(scope: Any, kind: str, selector: str | None = None) -> Any:
    if selector:
        return _unique(scope.locator(selector), f"{kind}文件输入框", visible=False)
    matches = []
    for item in scope.locator('input[type="file"]').all():
        accept = (item.get_attribute("accept") or "").lower()
        if kind == "video" and ("video" in accept or re.search(r"\.(mp4|mov|m4v|webm)", accept)):
            matches.append(item)
        elif kind == "image" and ("image" in accept or re.search(r"\.(png|jpe?g|webp)", accept)):
            matches.append(item)
    if len(matches) != 1:
        raise BrowserStepError(
            "file_input_not_identified",
            f"无法唯一识别 {kind} 上传框（找到 {len(matches)} 个）；需要对应 CSS selector。",
        )
    return matches[0]


def _normalize_name(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _metric(text: str) -> float | None:
    number = r"([\d,.]+)\s*([千万亿]?)"
    label = r"(?:热度|播放量|播放|观看|浏览|点赞)"
    match = re.search(label + r"\s*[:：]?\s*" + number, text)
    if not match:
        match = re.search(number + r"\s*(?:次)?\s*" + label, text)
    if not match:
        return None
    scale = {"": 1, "千": 1_000, "万": 10_000, "亿": 100_000_000}
    try:
        return float(match.group(1).replace(",", "")) * scale[match.group(2)]
    except ValueError:
        return None


def rank_collections(
    options: list[dict[str, Any]],
    keywords: list[str],
    preferred: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Rank actual account options, never invent a public/trending collection."""
    warnings: list[str] = []
    if preferred:
        wanted = _normalize_name(preferred)
        matches = [
            item for item in options
            if wanted in {_normalize_name(item["name"]), _normalize_name(item["text"])}
        ]
        if len(matches) == 1:
            return matches[0], warnings
        if len(matches) > 1:
            warnings.append(f"找到多个同名合集“{preferred}”，未自动选择。")
            return None, warnings
        warnings.append(f"当前账号的可选合集里未找到“{preferred}”。")
        return None, warnings

    relevant = []
    for index, option in enumerate(options):
        score = sum(1 for keyword in keywords if keyword and keyword.casefold() in option["text"].casefold())
        if score:
            relevant.append((option, score, index, _metric(option["text"])))
    if not relevant:
        keyword_text = "/".join(keyword for keyword in keywords if keyword) or "本条内容"
        warnings.append(f"未找到与{keyword_text}匹配的合集，未添加合集。")
        return None, warnings
    has_metrics = any(item[3] is not None for item in relevant)
    if has_metrics:
        selected = max(relevant, key=lambda item: (item[3] is not None, item[3] or 0, item[1], -item[2]))
        warnings.append("合集仅按页面显示的热度或播放数据排序；页面未证明这些数据代表近期热度。")
    else:
        selected = max(relevant, key=lambda item: (item[1], -item[2]))
        warnings.append("合集页面未显示可比较的热度或播放量；按本条内容关键词和列表顺序选择，不能判断近期热度。")
    return selected[0], warnings


def _description(manifest: dict[str, Any]) -> str:
    # Topic selection preserves the actual editor caption, including existing
    # user entities/plain tags.  Reconstructing it from tags duplicates them.
    if isinstance(manifest.get("prepared_caption"), str):
        return manifest["prepared_caption"]
    description = str(manifest.get("description") or "").strip()
    tags = []
    for raw in manifest.get("hashtags", []):
        tag = str(raw).strip().lstrip("#")
        if not tag or re.search(r"\s", tag):
            raise BrowserStepError("invalid_hashtag", "话题必须是非空且不含空白的名称。")
        if tag not in tags:
            tags.append(tag)
    return " ".join(part for part in [description, *(f"#{tag}" for tag in tags)] if part)


class _Publisher:
    def __init__(self, page: Any, manifest: dict[str, Any], config: dict[str, Any], timeout_seconds: int):
        self.page = page
        self.manifest = manifest
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.output_dir = Path(manifest["output_dir"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.report: dict[str, Any] = {
            "status": "working", "stage": "starting", "warnings": [],
            "job_id": manifest.get("job_id"), "started_at": _utc_now(),
            "checks": {}, "collection": None,
        }

    def screenshot(self, name: str) -> str | None:
        path = self.output_dir / name
        try:
            self.page.screenshot(path=str(path), full_page=True, timeout=15000)
            return str(path)
        except Exception as exc:
            self.report["warnings"].append(f"截图未能保存：{type(exc).__name__}")
            return None

    def control(self, key: str, fallback: Any, label: str) -> Any:
        locator = self.page.locator(self.config[key]) if self.config.get(key) else fallback
        return _unique(locator, label)

    def _body_text(self) -> str:
        try:
            return self.page.locator("body").inner_text(timeout=3000)
        except Exception:
            return ""

    def wait_for_upload_page(self) -> Any:
        self.report["stage"] = "authentication"
        self.page.goto(UPLOAD_URL, wait_until="domcontentloaded")
        started_at = time.monotonic()
        deadline = started_at + self.timeout_seconds
        prompted = False
        last_navigation = started_at
        while time.monotonic() < deadline:
            try:
                candidate = _file_input(self.page, "video", self.config.get("video_input"))
                return candidate
            except BrowserStepError:
                pass
            text = self._body_text()
            if not prompted:
                login_or_verification = re.search(
                    r"扫码登录|验证码登录|请先登录|请使用抖音扫码|登录后(?:发布|上传)|"
                    r"安全验证|请完成验证|滑块验证|拖动滑块|身份验证", text,
                )
                if login_or_verification:
                    print("请在打开的专用 Chrome 窗口中完成页面要求的登录或验证；程序会继续等待。", flush=True)
                    prompted = True
                elif time.monotonic() - started_at >= 10:
                    print("正在等待抖音上传页面加载；请查看专用 Chrome 窗口中的页面提示。", flush=True)
                    prompted = True
            # Authentication can redirect to home rather than the upload form.
            if (
                "/creator-micro/home" in self.page.url
                and not re.search(r"扫码登录|验证码登录|请先登录", text)
                and time.monotonic() - last_navigation > 5
            ):
                self.page.goto(UPLOAD_URL, wait_until="domcontentloaded")
                last_navigation = time.monotonic()
            self.page.wait_for_timeout(750)
        raise BrowserStepError("login_or_upload_page_timeout", "等待登录或视频上传页面超时。")

    def fill_metadata(self) -> None:
        self.report["stage"] = "metadata"
        title = str(self.manifest.get("title") or "").strip()
        if not title or len(title.encode("utf-16-le")) // 2 > 30:
            raise BrowserStepError("invalid_title", "作品标题不能为空，且不能超过 30 个 UTF-16 字符。")
        topic_settings = self.manifest.get("topic_selection") or {}
        if topic_settings.get("enabled"):
            # Seeds become real entities via search; never prefill duplicate
            # plain #text.  Existing live entities are preserved by add_topics.
            description = str(self.manifest.get("description") or "").strip()
        else:
            description = _description(self.manifest)
        if len(description.encode("utf-16-le")) // 2 > 1000:
            raise BrowserStepError("description_too_long", "作品简介和话题合计不能超过 1000 个 UTF-16 字符。")
        title_control = self.control(
            "title", self.page.get_by_placeholder("填写作品标题，为作品获得更多流量", exact=True), "作品标题",
        )
        title_control.fill(title)
        self.report["checks"]["title"] = title_control.input_value() == title
        editor = self.control(
            "description",
            self.page.locator('[contenteditable="true"][data-placeholder="添加作品简介"], '
                              '[contenteditable="true"][placeholder="添加作品简介"], '
                              'textarea[placeholder="添加作品简介"]'),
            "作品简介",
        )
        # Filling can remove the placeholder attribute that identified this
        # editor. Keep the uniquely identified node instead of querying again.
        editor_handle = editor.element_handle()
        if editor_handle is None:
            raise BrowserStepError("description_not_confirmed", "作品简介编辑框已消失，无法核对填写内容。")
        try:
            editor_handle.fill(description)
            current = editor_handle.evaluate("""el => {
                if (!el.isConnected) return null;
                if (['INPUT', 'TEXTAREA'].includes(el.tagName)) return el.value;
                if (el.getAttribute('data-slate-editor') !== 'true') return el.innerText;
                // Slate appends an empty paragraph sentinel. Ignore only this
                // exact marker, preserving zero-width characters in real text.
                const clone = el.cloneNode(true);
                let removedEmptyMarker = false;
                for (const marker of clone.querySelectorAll('span[data-string="true"][data-enter="true"]')) {
                    if (marker.childElementCount === 0 && marker.textContent === '\\u200b') {
                        marker.remove();
                        removedEmptyMarker = true;
                    }
                }
                return removedEmptyMarker ? clone.textContent : el.innerText;
            }""")
        finally:
            editor_handle.dispose()
        if not isinstance(current, str):
            raise BrowserStepError("description_not_confirmed", "作品简介编辑框已被替换，无法核对填写内容。")
        if _normalize_name(current) != _normalize_name(description):
            raise BrowserStepError("description_not_confirmed", "页面中的作品简介与准备内容不一致。")
        self.report["checks"]["description"] = True
        self.report["hashtags"] = self.manifest.get("hashtags", [])

    def add_topics(self) -> None:
        settings = self.manifest.get("topic_selection") or {}
        if not settings.get("enabled"):
            return
        from .topics import select_topics
        self.report["stage"] = "topic_selection"
        evidence: dict[str, Any] = {}
        self.report["topic_selection"] = evidence
        self.report["checks"]["topics"] = False
        settings = dict(settings)
        manual_seeds = self.manifest.get("hashtags", [])
        settings["queries"] = list(dict.fromkeys([*settings.get("queries", []), *manual_seeds]))
        if settings.get("keywords"):
            # Explicit user topic seeds are relevant even when their names do
            # not contain the analyzed keywords. Keep allow/exclude constraints;
            # an empty keywords list still falls back to the complete queries.
            settings["keywords"] = list(dict.fromkeys([*settings["keywords"], *manual_seeds]))
        select_topics(self.page, settings, report=evidence,
                      editor_selector=self.config.get("description") or '[contenteditable="true"][data-slate-editor="true"]',
                      timeout_seconds=self.config.get("topic_timeout_seconds", 3))
        self.manifest["hashtags"] = list(evidence["actual_topics"])
        self.manifest["prepared_caption"] = evidence["actual_caption"]
        self.manifest["topic_selection_result"] = evidence
        self.report["hashtags"] = list(evidence["actual_topics"])
        self.report["checks"]["topics"] = True
        if self.config.get("_manifest_path"):
            from .jobs import write_json
            write_json(Path(self.config["_manifest_path"]), self.manifest)

    def wait_upload_ready(self) -> None:
        self.report["stage"] = "video_processing"
        deadline = time.monotonic() + int(self.config.get("upload_timeout_seconds", self.timeout_seconds))
        while time.monotonic() < deadline:
            text = self._body_text()
            if re.search(r"上传失败|视频处理失败|转码失败|文件格式不支持|视频格式不支持", text):
                raise BrowserStepError("video_upload_failed", "页面报告视频上传或处理失败。")
            if self.config.get("upload_ready"):
                ready = _has_visible(self.page.locator(self.config["upload_ready"]))
            else:
                success = _has_visible(self.page.get_by_text(re.compile(r"^(上传成功|重新上传|重新上传视频)$")))
                # Local blob previews can exist before upload finishes. Require
                # an explicit completion marker and inspect actual status labels,
                # not help sentences explaining what happens while uploading.
                processing = _has_visible(self.page.get_by_text(re.compile(
                    r"^\s*(?:(?:视频)?(?:正在)?上传中|正在上传(?:视频)?|"
                    r"正在转码|转码中|视频处理中|正在处理视频|正在校验视频|"
                    r"等待上传|等待转码|等待视频处理|文件解析中)"
                    r"(?:[，,]\s*请稍等)?"
                    r"(?:\s*[:：]?\s*(?:\d+(?:\.\d+)?\s*%|[（(]\s*\d+(?:\.\d+)?\s*%\s*[)）]))?"
                    r"[.。…]*\s*$",
                )))
                ready = success and not processing
            if ready:
                self.report["checks"]["video_processed"] = True
                return
            self.page.wait_for_timeout(750)
        raise BrowserStepError("video_processing_timeout", "未能确认视频上传和处理完成；不会点击发布。")

    def _cover_scope(self) -> tuple[Any, bool]:
        if self.config.get("cover_dialog"):
            return _unique(self.page.locator(self.config["cover_dialog"]), "封面编辑窗口"), True
        dialogs = _visible(self.page.locator('[role="dialog"], .semi-modal-content, .ant-modal-content'))
        if len(dialogs) == 1:
            return dialogs[0], True
        if len(dialogs) > 1:
            raise BrowserStepError("ambiguous_cover_dialog", "出现多个封面窗口，需要配置 cover_dialog。")
        return self.page, False

    def set_cover(self, orientation: str) -> None:
        self.report["checks"].pop(f"cover_{orientation}", None)
        self.report.setdefault("cover_evidence", {})[orientation] = {}
        try:
            self._set_verified_cover(orientation)
        except Exception:
            self.report["cover_evidence"][orientation]["error_screenshot"] = self.screenshot(f"cover-{orientation}-error.png")
            raise

    def _cover_preview_evidence(self, scope: Any, selector: str) -> dict[str, Any] | None:
        matches = scope.locator(selector)
        if matches.count() > 1:
            raise BrowserStepError("ambiguous_cover_evidence", "封面证据 selector 出现多个匹配；请定位唯一实际预览。")
        visible = _visible(matches)
        if not visible:
            return None
        node = visible[0]
        placeholder = node.evaluate("""el => {
            const card = el.closest('[class*="coverControl-"]');
            return /选择封面/.test((card || el).innerText || '');
        }""")
        visuals = node.locator("img,canvas")
        if node.evaluate("el => ['IMG', 'CANVAS'].includes(el.tagName)"):
            preview = node
        else:
            candidates = _visible(visuals)
            if len(candidates) != 1:
                return None
            preview = candidates[0]
        info = preview.evaluate("""el => ({
            kind:el.tagName.toLowerCase(), source:el.currentSrc || el.src || null,
            width:el.naturalWidth || el.width, height:el.naturalHeight || el.height,
            ready:el.tagName === 'IMG' ? el.complete && el.naturalWidth > 0 : el.width > 0 && el.height > 0
        })""")
        if not info["ready"]:
            return None
        info["placeholder"] = placeholder
        # A URL, filename or input value alone does not prove changed artwork.
        info["pixel_sha256"] = hashlib.sha256(preview.screenshot(timeout=3000, animations="disabled")).hexdigest()
        return info

    def _set_verified_cover(self, orientation: str, *, opened_scope: Any | None = None) -> None:
        self.report["stage"] = f"cover_{orientation}"
        path = Path(self.manifest["covers"][orientation])
        if not path.is_file():
            raise BrowserStepError("cover_not_found", f"封面文件不存在：{path}")
        imported_selector = self.config.get(f"cover_{orientation}_imported") or self.config.get("cover_imported")
        ready_key = f"cover_{orientation}_ready"
        evidence = self.report.setdefault("cover_evidence", {}).setdefault(orientation, {})
        evidence.update(source_file=str(path), source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        imported_selector=imported_selector, applied_selector=self.config.get(ready_key))
        if not imported_selector or not self.config.get(ready_key):
            raise BrowserStepError(
                "cover_verification_not_configured",
                f"需要 cover_imported（或 cover_{orientation}_imported）和 {ready_key} 指向实际图片/画布，核对导入与应用。",
            )
        applied_before = self._cover_preview_evidence(self.page, self.config[ready_key])
        evidence["applied_before"] = applied_before
        if opened_scope is None:
            key = f"cover_{orientation}_trigger"
            if self.config.get(key):
                trigger = _unique(self.page.locator(self.config[key]), "封面入口")
            else:
                trigger = self._cover_trigger(orientation)
            trigger.click()
        scope, is_dialog = (opened_scope, True) if opened_scope is not None else (self.page, False)

        def identify_image_input() -> Any | None:
            if self.config.get("cover_image_input"):
                if not scope.locator(self.config["cover_image_input"]).count():
                    return None
                return _file_input(scope, "image", self.config["cover_image_input"])
            # The editor also has AI reference-image and replacement inputs.
            # Only the visible local-cover dropzone identifies our target.
            dropzones = _visible(scope.locator(".semi-upload").filter(
                has=self.page.get_by_text("点击上传文件或拖拽文件到这里", exact=True),
            ))
            if len(dropzones) > 1:
                raise BrowserStepError(
                    "file_input_not_identified",
                    f"无法唯一识别本地封面上传区域（找到 {len(dropzones)} 个）；需要 cover_image_input selector。",
                )
            if len(dropzones) == 1:
                primary = ':scope > input[type="file"].semi-upload-hidden-input:not(.semi-upload-hidden-input-replace)'
                if not dropzones[0].locator(primary).count():
                    return None
                return _file_input(
                    dropzones[0], "image", primary,
                )
            image_inputs = [item for item in scope.locator('input[type="file"]').all()
                            if "image" in (item.get_attribute("accept") or "").lower()
                            or re.search(r"\.(png|jpe?g|webp)", (item.get_attribute("accept") or "").lower())]
            if not image_inputs:
                return None
            candidate = _file_input(scope, "image")
            if candidate.evaluate("""el =>
                el.classList.contains('semi-upload-hidden-input-replace') || !!el.closest('.semi-upload')
            """):
                raise BrowserStepError(
                    "file_input_not_identified",
                    "现有图片输入框未能确认为本地封面上传区域；需要 cover_image_input selector。",
                )
            return candidate

        input_deadline = time.monotonic() + min(30, self.timeout_seconds)
        tab_clicked = False
        image_input = None
        while time.monotonic() < input_deadline:
            if self.config.get("cover_dialog"):
                dialogs = _visible(self.page.locator(self.config["cover_dialog"]))
                if len(dialogs) > 1:
                    raise BrowserStepError("ambiguous_cover_dialog", "配置的封面编辑窗口出现多个可见匹配。")
                if not dialogs:
                    self.page.wait_for_timeout(100)
                    continue
                scope, is_dialog = dialogs[0], True
            else:
                # The dialog may appear after the click handler and React render.
                # Refresh the scope instead of retaining an early whole-page scope.
                scope, is_dialog = self._cover_scope()
            if not is_dialog:
                self.page.wait_for_timeout(100)
                continue
            # Existing upload inputs can overlay the upload label. Setting the
            # file directly avoids a blocked click or a native file chooser.
            image_input = identify_image_input()
            if image_input is not None:
                break
            if not tab_clicked:
                if self.config.get("cover_local_tab"):
                    tabs = _visible(scope.locator(self.config["cover_local_tab"]))
                    if len(tabs) > 1:
                        raise BrowserStepError("ambiguous_control", "上传本地封面入口出现多个可见匹配。")
                    if tabs:
                        tabs[0].click()
                        tab_clicked = True
                elif not scope.locator('input[type="file"]').count():
                    for label in ("上传封面", "本地上传", "本地图片", "上传图片"):
                        candidates = _visible(scope.get_by_text(label, exact=True))
                        if len(candidates) == 1:
                            candidates[0].click()
                            tab_clicked = True
                            break
            self.page.wait_for_timeout(100)
        if image_input is None:
            raise BrowserStepError(
                "file_input_not_identified", "等待可确认的封面编辑窗口或唯一图片上传框超时；请检查 cover_dialog / cover_image_input。",
            )
        imported_before = self._cover_preview_evidence(scope, imported_selector)
        evidence["imported_before"] = imported_before
        evidence["input"] = image_input.evaluate("""el => ({id:el.id, class:el.className, accept:el.accept})""")
        evidence["input_selector"] = self.config.get("cover_image_input") or "unique local-cover dropzone input"
        # The actual uploader clears its input synchronously after a selection.
        # Hold the original File in a one-shot capture listener so its hash can
        # still be checked after React clears or remounts the control.
        input_element = image_input.element_handle()
        if input_element is None:
            raise BrowserStepError("cover_file_not_confirmed", "封面上传框已消失，无法核对选择文件。")
        try:
            input_element.evaluate("""el => {
                el.__douyinCoverProof = new Promise(resolve => {
                    el.addEventListener('change', async () => {
                        const file = el.files && el.files.length === 1 ? el.files[0] : null;
                        if (!file) { resolve(null); return; }
                        try {
                            const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
                            resolve({name:file.name, sha256:Array.from(new Uint8Array(digest),
                                b => b.toString(16).padStart(2,'0')).join('')});
                        } catch (_) { resolve(null); }
                    }, {once:true, capture:true});
                });
            }""")
            input_element.set_input_files(str(path))
            selected = input_element.evaluate("""async el => {
                const proof = await el.__douyinCoverProof;
                delete el.__douyinCoverProof;
                return proof;
            }""")
        finally:
            input_element.dispose()
        if not selected or selected["sha256"] != evidence["source_sha256"]:
            raise BrowserStepError("cover_file_not_confirmed", "唯一封面上传框中的文件与本地所选文件不一致。")
        evidence["selected_file"] = selected

        def wait_for_new_preview(target_scope: Any, selector: str, before: dict | None, phase: str) -> dict:
            deadline = time.monotonic() + min(self.timeout_seconds, int(self.config.get("cover_evidence_timeout_seconds", 30)))
            while time.monotonic() < deadline:
                current = self._cover_preview_evidence(target_scope, selector)
                # After saving both steps, Douyin can briefly copy the portrait
                # asset into the landscape card before its own asset arrives.
                # That changed image is not proof of the landscape upload.
                if current and target_scope is self.page and current["kind"] == "img":
                    ratio = current["width"] / current["height"]
                    expected_ratio = 3 / 4 if orientation == "vertical" else 4 / 3
                    if abs(ratio / expected_ratio - 1) > 0.03:
                        self.page.wait_for_timeout(100)
                        continue
                if current and not current["placeholder"] and (before is None or current["pixel_sha256"] != before["pixel_sha256"]):
                    if before is None or current["kind"] != "img" or current["source"] != before["source"]:
                        return current
                self.page.wait_for_timeout(100)
            raise BrowserStepError("cover_not_confirmed", f"未能确认本次自定义封面已{phase}；旧预览、按钮可用或窗口关闭均不能证明成功。")

        evidence["imported_after"] = wait_for_new_preview(scope, imported_selector, imported_before, "导入编辑器")
        evidence["imported_screenshot"] = self.screenshot(f"cover-{orientation}-imported.png")
        # Douyin can present one editor with two steps. The portrait step's
        # disabled Done button is not the save action: upload portrait, advance
        # to landscape, upload that artwork, then save both together. Do not
        # alias the advance button to cover_confirm, which would hide a failed
        # second step and incorrectly mark the portrait as applied.
        next_buttons = (
            scope.locator(self.config["cover_next"])
            if self.config.get("cover_next")
            else scope.get_by_role("button", name="设置横封面", exact=True)
        )
        if orientation == "vertical" and _has_visible(next_buttons):
            next_button = _unique(next_buttons, "设置横封面步骤")
            if not next_button.is_enabled():
                raise BrowserStepError("cover_processing_timeout", "竖封面导入后，横封面步骤仍不可用。")
            evidence["editor_flow"] = "portrait_then_landscape"
            next_button.click()
            self._set_verified_cover("horizontal", opened_scope=scope)
            evidence["applied_after"] = wait_for_new_preview(
                self.page, self.config[ready_key], applied_before, "应用至主页面",
            )
            evidence["applied_screenshot"] = self.screenshot("cover-vertical-applied.png")
            self.report["checks"]["cover_vertical"] = True
            return
        if self.config.get("cover_confirm"):
            confirm = _unique(scope.locator(self.config["cover_confirm"]), "应用封面按钮")
        else:
            confirm = _unique(
                scope.get_by_role("button", name=re.compile(r"^(完成|确定|确认|应用)$")), "应用封面按钮",
            )
        confirm.wait_for(state="visible", timeout=30000)
        deadline = time.monotonic() + 60
        while not confirm.is_enabled() and time.monotonic() < deadline:
            self.page.wait_for_timeout(250)
        if not confirm.is_enabled():
            raise BrowserStepError("cover_processing_timeout", "封面处理后应用按钮仍不可用。")
        confirm.click()
        if is_dialog:
            try:
                scope.wait_for(state="hidden", timeout=30000)
            except Exception as exc:
                raise BrowserStepError("cover_not_confirmed", "应用封面后编辑窗口未关闭。") from exc
        evidence["applied_after"] = wait_for_new_preview(self.page, self.config[ready_key], applied_before, "应用至主页面")
        evidence["applied_screenshot"] = self.screenshot(f"cover-{orientation}-applied.png")
        self.report["checks"][f"cover_{orientation}"] = True

    def _cover_trigger(self, orientation: str) -> Any:
        """Find the orientation card even after the other card changes its label."""
        button_text = re.compile(r"^(选择封面|更换封面|修改封面|编辑封面)$")
        label_text = re.compile(r"^" + ("竖封面" if orientation == "vertical" else "横封面") + r"(?:\s*[34]\s*[:：/]\s*[34])?$")
        labels = _visible(self.page.get_by_text(label_text))
        if len(labels) == 1:
            ancestor = labels[0]
            for _ in range(4):
                ancestor = ancestor.locator("..")
                candidates = _visible(ancestor.get_by_text(button_text))
                if len(candidates) == 1:
                    return candidates[0]
                if len(candidates) > 1:
                    break
        choices = _visible(self.page.get_by_text(button_text))
        if len(choices) == 2:
            return choices[0 if orientation == "vertical" else 1]
        # A completed vertical card can become an image without a text control.
        remaining = _visible(self.page.get_by_text("选择封面", exact=True))
        if orientation == "horizontal" and self.report["checks"].get("cover_vertical") and len(remaining) == 1:
            return remaining[0]
        raise BrowserStepError("cover_trigger_not_identified", "无法区分横竖封面入口；请配置对应 cover_*_trigger selector。")

    def choose_collection(self) -> None:
        self.report["stage"] = "collection"
        settings = self.manifest.get("collection") or {}
        if not settings.get("preferred") and not settings.get("keywords"):
            if settings.get("required"):
                raise BrowserStepError("required_collection_missing", "请明确指定此账号中的合集名称。")
            return
        trigger_locator = (
            self.page.locator(self.config["collection_trigger"]) if self.config.get("collection_trigger")
            else self.page.get_by_text("请选择合集", exact=True)
        )
        triggers = _visible(trigger_locator)
        if len(triggers) != 1:
            if settings.get("required"):
                raise BrowserStepError("collection_not_available", "无法定位账号的合集选项，但此任务要求添加合集。")
            self.report["warnings"].append("未找到明确的合集入口，未添加合集。")
            return
        triggers[0].click()
        self.page.wait_for_timeout(300)
        if self.config.get("collection_options"):
            options_locator = self.page.locator(self.config["collection_options"])
        else:
            options_locator = self.page.get_by_role("option")
            if not _visible(options_locator):
                options_locator = self.page.locator(
                    '.semi-select-option, .ant-select-item-option, [role="listbox"] [data-option-value]',
                )
        options = []
        for option in _visible(options_locator):
            text = _text(option)
            if text and not re.search(r"^(创建|新建|暂无|没有)", text):
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                options.append({"name": lines[0], "text": text, "locator": option})
        choice, warnings = rank_collections(
            options, settings.get("keywords") or [], settings.get("preferred"),
        )
        self.report["warnings"].extend(warnings)
        if choice is None:
            self.page.keyboard.press("Escape")
            if settings.get("required"):
                raise BrowserStepError("required_collection_missing", "任务要求的合集未能选定。")
            return
        choice["locator"].click()
        deadline = time.monotonic() + 5
        confirmed = False
        while time.monotonic() < deadline:
            if self.config.get("collection_ready"):
                selected = _visible(self.page.locator(self.config["collection_ready"]))
            elif self.config.get("collection_trigger"):
                selected = _visible(self.page.locator(self.config["collection_trigger"]))
            else:
                selected = _visible(self.page.get_by_text(choice["name"], exact=True))
            confirmed = any(choice["name"] in _text(item) for item in selected)
            # An option that remains open is not proof of a selected value.
            if confirmed and not _has_visible(choice["locator"]):
                break
            confirmed = False
            self.page.wait_for_timeout(100)
        if not confirmed:
            raise BrowserStepError("collection_not_confirmed", "点击合集后未能确认页面已经选中该合集。")
        self.report["collection"] = {"name": choice["name"], "visible_text": choice["text"], "metric": _metric(choice["text"])}
        self.report["checks"]["collection"] = True

    def fill_declaration(self) -> None:
        declaration = self.manifest.get("declaration")
        if not declaration:
            return
        self.report["stage"] = "declaration"
        self.control("declaration_trigger", self.page.get_by_text("请选择自主声明", exact=True), "自主声明入口").click()
        locator = (
            self.page.locator(self.config["declaration_options"]) if self.config.get("declaration_options")
            else self.page.get_by_text(str(declaration), exact=True)
        )
        if self.config.get("declaration_options"):
            locator = locator.filter(has_text=re.compile(r"^" + re.escape(str(declaration)) + r"$"))
        _unique(locator, "配置的自主声明").click()
        self.report["checks"]["declaration"] = True

    def preflight(self) -> None:
        checks = self.report["checks"]
        required = ["title", "description", "video_processed", "cover_vertical", "cover_horizontal"]
        if (self.manifest.get("collection") or {}).get("required"):
            required.append("collection")
        if self.manifest.get("declaration"):
            required.append("declaration")
        if (self.manifest.get("topic_selection") or {}).get("enabled"):
            required.append("topics")
            from .topics import editor_snapshot
            selector = self.config.get("description") or '[contenteditable="true"][data-slate-editor="true"]'
            editors = self.page.locator(selector)
            if editors.count() != 1:
                raise BrowserStepError("topic_editor_ambiguous", "发布前无法核对唯一话题编辑框。")
            actual = editor_snapshot(editors)
            if (actual["text"] != self.manifest.get("prepared_caption")
                    or actual["entities"] != self.manifest.get("hashtags")):
                raise BrowserStepError("topics_changed", "话题准备后正文或实体发生变化；请重新核对。")
        missing = [key for key in required if not checks.get(key)]
        if missing:
            raise BrowserStepError("preflight_failed", "发布前检查未通过：" + ", ".join(missing))
        errors = self.page.locator('[role="alert"], .semi-form-field-error-message, .ant-form-item-explain-error').filter(
            has_text=re.compile(r"请填写作品标题|请设置封面|请选择合集|请完成自主声明|标题不能为空"),
        )
        if _has_visible(errors):
            raise BrowserStepError("required_fields_missing", "页面仍提示必填信息缺失；不会发布。")

    def submit(self) -> None:
        self.report["stage"] = "publish"
        self.preflight()
        button = self.control("publish_button", self.page.get_by_role("button", name="发布", exact=True), "发布按钮")
        if not button.is_enabled():
            raise BrowserStepError("publish_disabled", "发布按钮尚不可用；不会提交。")
        attempt_path = self.output_dir / "publish-attempt.json"
        attempt = {
            "job_id": self.manifest.get("job_id"), "title": self.manifest["title"],
            "video": self.manifest["video"], "attempted_at": _utc_now(),
            "state": "publish_attempted", "note": "Written before clicking; inspect account before any retry.",
        }
        try:
            with attempt_path.open("x", encoding="utf-8") as handle:
                json.dump(attempt, handle, ensure_ascii=False, indent=2)
                handle.flush()
                import os
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise BrowserStepError("duplicate_publish_attempt", "已有发布尝试记录；请先检查抖音作品管理，避免重复发布。") from exc
        self.report["publish_attempt"] = str(attempt_path)
        self.report["status"] = "publish_attempted"
        button.click()
        deadline = time.monotonic() + int(self.config.get("success_timeout_seconds", 90))
        while time.monotonic() < deadline:
            success = False
            evidence = "页面明确显示发布成功状态"
            on_management_page = bool(re.match(
                r"^https://creator\.douyin\.com/creator-micro/content/manage(?:[/?#]|$)", self.page.url,
            ))
            if on_management_page:
                expected_caption = _normalize_name(self.manifest["title"] + " " + _description(self.manifest))
                matches = []
                for card in _visible(self.page.locator('div[class*="video-card-info-"]')):
                    titles = _visible(card.locator('[class*="info-title-text-"]'))
                    if len(titles) != 1 or _normalize_name(_text(titles[0])) != expected_caption:
                        continue
                    matches.append((card, _text(titles[0])))
                if len(matches) > 1:
                    raise BrowserStepError("ambiguous_published_work", "作品列表出现多个完全同文案作品，无法唯一确认本次发布结果。")
                if matches:
                    card, caption = matches[0]
                    status = _text(_unique(card.locator('[class*="info-status-"]'), "目标作品审核状态"))
                    if status in {"审核中", "已发布"}:
                        success = True
                        self.report["platform_status"] = "under_review" if status == "审核中" else "published"
                        times = _visible(card.locator('[class*="info-time-"]'))
                        self.report["publication"] = {
                            "url": self.page.url, "title_and_description": caption, "status_text": status,
                            "published_at_text": _text(times[0]) if len(times) == 1 else None,
                        }
                        evidence = "作品管理中唯一匹配标题和完整简介的目标作品，状态为" + status
                    elif status in {"审核未通过", "未通过审核", "发布失败", "提交失败"}:
                        raise BrowserStepError("publish_rejected", "目标作品状态为" + status + "；请核对作品管理，勿直接重试。")
            if not success:
                if self.config.get("publish_success"):
                    success = _has_visible(self.page.locator(self.config["publish_success"]))
                else:
                    # Help text contains “等待上传发布完成”, which is not a
                    # publication result. Accept only a complete status message.
                    success = _has_visible(self.page.get_by_text(re.compile(
                        r"^(?:发布成功|作品已发布|作品发布成功|发布完成)[！!。]?$",
                    )))
            if success:
                self.report["status"] = "published"
                self.report["success_evidence"] = evidence
                self.report["screenshot"] = self.screenshot("published.png")
                return
            if not on_management_page and re.search(r"发布失败|提交失败|审核未通过", self._body_text()):
                raise BrowserStepError("publish_rejected", "页面报告发布失败；已保留尝试记录，请人工检查后再决定是否重试。")
            self.page.wait_for_timeout(750)
        raise BrowserStepError("publish_result_unknown", "已点击发布，但未看到明确的发布成功状态；请检查作品管理，勿直接重试。")

    def run(self, publish: bool) -> dict[str, Any]:
        if publish and (self.output_dir / "publish-attempt.json").exists():
            raise BrowserStepError("duplicate_publish_attempt", "已有发布尝试记录；请先检查抖音作品管理，避免重复发布。")
        video = Path(self.manifest["video"])
        if not video.is_file():
            raise BrowserStepError("video_not_found", f"视频文件不存在：{video}")
        video_input = self.wait_for_upload_page()
        self.report["stage"] = "upload"
        video_input.set_input_files(str(video))
        # Metadata controls can take time to appear after file selection.
        title_selector = self.config.get("title")
        title = self.page.locator(title_selector) if title_selector else self.page.get_by_placeholder("填写作品标题，为作品获得更多流量", exact=True)
        title.first.wait_for(state="visible", timeout=min(self.timeout_seconds * 1000, 120000))
        self.fill_metadata()
        self.add_topics()
        self.wait_upload_ready()
        self.set_cover("vertical")
        if not self.report["checks"].get("cover_horizontal"):
            self.set_cover("horizontal")
        self.choose_collection()
        self.fill_declaration()
        self.preflight()
        self.report["screenshot"] = self.screenshot("ready-to-publish.png")
        if publish:
            self.submit()
        else:
            self.report.update(status="filled", stage="review", published=False)
            if self.config.get("review", True) and sys.stdin.isatty():
                print("视频和资料已填写；尚未点击发布。请在浏览器检查，按 Enter 关闭专用浏览器。", flush=True)
                input()
                self.report["review"] = "terminal_review_finished"
            else:
                self.report["review"] = "screenshot_only"
                self.report["warnings"].append("未进入终端人工检查；截图后将关闭浏览器，未保证页面自动保存草稿。")
        return self.report


def _launch(playwright: Any, profile_dir: Path, channel: str) -> Any:
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.chmod(0o700)
    context = playwright.chromium.launch_persistent_context(
        str(profile_dir), channel=channel, headless=False,
        viewport={"width": 1360, "height": 1050}, locale="zh-CN",
    )
    context.set_default_timeout(15000)
    context.set_default_navigation_timeout(60000)
    return context


def _save_report(report: dict[str, Any], output_dir: Path) -> None:
    def strip_preview_queries(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "source" and isinstance(item, str) and item.startswith(("https://", "http://")):
                    value[key] = item.split("?", 1)[0].split("#", 1)[0]
                else:
                    strip_preview_queries(item)
        elif isinstance(value, list):
            for item in value:
                strip_preview_queries(item)

    # Private preview media URLs can contain temporary auth query parameters.
    # The image path and pixel evidence suffice for the durable task report.
    strip_preview_queries(report)
    report["finished_at"] = _utc_now()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "browser-report.json"
    report["report_path"] = str(path)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def validate_cover_config(config: dict[str, Any]) -> None:
    """Reject a fresh installation's missing selectors before any upload."""
    for orientation in ("vertical", "horizontal"):
        imported = config.get(f"cover_{orientation}_imported") or config.get("cover_imported")
        if not imported or not config.get(f"cover_{orientation}_ready"):
            raise BrowserStepError("cover_verification_not_configured",
                                   "首次使用请配置封面编辑预览和横竖主预览选择器，参见 selectors.example.json。")


def run_publish(
    manifest: dict[str, Any],
    profile_dir: Path,
    *,
    publish: bool = False,
    channel: str = "chrome",
    timeout_seconds: int = 600,
    selector_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fill a real form, or publish only when ``publish=True`` is explicit.

    Returned ``publish_attempted`` means the outcome needs an account check, not a
    retry. A successful click alone never produces ``published``.
    """
    from playwright.sync_api import sync_playwright

    config = selector_config or {}
    validate_cover_config(config)
    output_dir = Path(manifest["output_dir"])
    report: dict[str, Any] = {"status": "failed", "stage": "browser_start", "warnings": []}
    publisher: _Publisher | None = None
    context = None
    try:
        with sync_playwright() as playwright:
            try:
                context = _launch(playwright, Path(profile_dir), channel)
                page = context.pages[0] if context.pages else context.new_page()
                publisher = _Publisher(page, manifest, config, timeout_seconds)
                report = publisher.run(publish)
            except Exception as exc:
                if publisher:
                    report = publisher.report
                    report["screenshot"] = publisher.screenshot("browser-error.png")
                if report.get("status") != "publish_attempted":
                    report["status"] = "failed"
                report["error"] = {"code": getattr(exc, "code", "browser_error"), "message": str(exc)}
            finally:
                if context:
                    try:
                        context.close()
                    except Exception:
                        pass
    except Exception as exc:
        report["error"] = {"code": "playwright_start_failed", "message": str(exc)}
    report["browser_closed"] = True
    _save_report(report, output_dir)
    return report


def login(profile_dir: Path, *, channel: str = "chrome", timeout_seconds: int = 300) -> dict[str, Any]:
    """Open the dedicated browser and wait for a usable authenticated upload form."""
    from playwright.sync_api import sync_playwright

    report: dict[str, Any] = {"status": "failed", "profile_dir": str(profile_dir)}
    context = None
    try:
        with sync_playwright() as playwright:
            try:
                context = _launch(playwright, Path(profile_dir), channel)
                page = context.pages[0] if context.pages else context.new_page()
                # A video upload input is stronger authentication evidence than a
                # URL or a navigation label also present on the public landing page.
                page.goto(HOME_URL, wait_until="domcontentloaded")
                worker = _Publisher(page, {"output_dir": str(profile_dir / "diagnostics")}, {}, timeout_seconds)
                worker.wait_for_upload_page()
                report["status"] = "authenticated"
                report["message"] = "已确认登录，可使用该专用浏览器配置上传视频。"
            except Exception as exc:
                report["error"] = {"code": getattr(exc, "code", "browser_error"), "message": str(exc)}
                if context and context.pages:
                    path = Path(profile_dir) / "login-error.png"
                    try:
                        context.pages[0].screenshot(path=str(path), full_page=True)
                        report["screenshot"] = str(path)
                    except Exception:
                        pass
            finally:
                if context:
                    try:
                        context.close()
                    except Exception:
                        pass
    except Exception as exc:
        report["error"] = {"code": "playwright_start_failed", "message": str(exc)}
    return report
