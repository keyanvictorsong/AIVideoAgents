"""Offline browser integration: every URL is intercepted before navigation."""

import base64
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from PIL import Image, ImageDraw

from xhs_publisher.browser import BrowserStepError, _Preparer, UPLOAD_URL
from xhs_publisher.cover_validation import compare_cover
from xhs_publisher.session import SESSION_FILENAME, SessionStateError

FIXTURE = Path(__file__).parent / "fixtures" / "creator.html"
SELECTORS = {
    "video_input": "#video", "title": "#title", "description": "#description",
    "upload_ready": "#upload-status", "cover_trigger": "#cover-open",
    "cover_dialog": "#cover-dialog", "cover_input": "#cover-file",
    "cover_imported": "#imported-preview", "cover_confirm": "#apply-cover",
    "cover_ready": "#cover-preview", "publish_button": "#publish",
}


class BrowserFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            cls.playwright.stop()
            raise unittest.SkipTest(f"Chrome unavailable for offline integration: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        video = self.root / "video.mp4"
        video.write_bytes(b"offline fixture video; never sent over a network")
        cover = self.root / "vertical.png"
        picture = Image.new("RGB", (120, 160), "#cc3322")
        ImageDraw.Draw(picture).rectangle((15, 20, 85, 110), fill="#22aa55")
        picture.save(cover)
        self.manifest = {
            "job_id": "offline-fixture", "video": str(video), "cover": str(cover),
            "title": "学习像旅行：认真思考", "description": "先理解内容，再练习。",
            "tags": ["示例", "示例教学"], "output_dir": str(self.root / "output"),
            "source": {"sha256": hashlib.sha256(video.read_bytes()).hexdigest()},
            "cover_source": {"sha256": hashlib.sha256(cover.read_bytes()).hexdigest()},
        }
        self.context = self.browser.new_context()
        self.context.route("**/*", lambda route: route.fulfill(
            body=FIXTURE.read_text(encoding="utf-8"), content_type="text/html",
        ))
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        self.preparer = _Preparer(self.page, self.manifest, dict(SELECTORS), timeout_seconds=1)

    def tearDown(self):
        self.context.close()
        self.folder.cleanup()

    def upload(self):
        self.preparer.upload_video()

    def assert_not_published(self):
        self.assertEqual(self.page.evaluate("state.published"), 0)
        self.assertFalse(self.preparer.report.get("published", False))

    def test_prepare_verifies_materials_and_leaves_final_publish_for_user(self):
        report = self.preparer.prepare()
        self.assertEqual(report["status"], "awaiting_user_publish")
        self.assertIs(report["published"], False)
        for check in ("title", "description", "video_processed", "cover"):
            self.assertIs(report["checks"][check], True)
        self.assertEqual(self.page.locator("#title").input_value(), self.manifest["title"])
        self.assertEqual(self.page.locator("#description").inner_text().split(),
                         ["先理解内容，再练习。", "#示例", "#示例教学"])
        self.assertEqual(self.page.evaluate("state.videoFiles"), ["video.mp4"])
        self.assertEqual(self.page.evaluate("state.coverFiles"), ["vertical.png"])
        self.assertEqual(self.page.locator("#unrelated").evaluate("el => el.files.length"), 0)
        marker = self.root / "output" / "manual-handoff.json"
        self.assertTrue(marker.is_file())
        self.assertFalse(json.loads(marker.read_text(encoding="utf-8")).get("published", False))
        self.assertTrue((self.root / "output" / "ready-to-publish.png").is_file())
        self.assert_not_published()

    def test_ambiguous_metadata_controls_are_rejected_without_filling(self):
        for field in ("title", "description"):
            with self.subTest(field=field):
                self.preparer.wait_for_upload_page()
                self.page.locator("#video").set_input_files(self.manifest["video"])
                self.preparer.config[field] = f"#{field}, #duplicate"
                self.page.locator(f"#{field}").evaluate("""el => {
                    const duplicate = el.cloneNode(true);
                    duplicate.id = 'duplicate';
                    el.after(duplicate);
                }""")
                with self.assertRaises(BrowserStepError):
                    self.preparer.fill_metadata()
                editor = self.page.locator(f"#{field}")
                self.assertEqual(editor.input_value() if field == "title" else editor.inner_text(), "")
                self.assert_not_published()
                self.preparer.config = dict(SELECTORS)

    def test_title_modified_by_page_is_rejected(self):
        self.upload()
        self.page.locator("#title").evaluate("""el => {
            el.addEventListener('input', () => { el.value = el.value.slice(0, 5); });
        }""")
        with self.assertRaises(BrowserStepError):
            self.preparer.fill_metadata()
        self.assertNotEqual(self.page.locator("#title").input_value(), self.manifest["title"])
        self.assertFalse(self.preparer.report["checks"].get("title", False))
        self.assert_not_published()

    def test_description_modified_after_placeholder_removed_is_rejected(self):
        self.upload()
        self.page.locator("#description").evaluate("""el => {
            el.addEventListener('input', () => { el.textContent = '页面替换后的不同正文'; });
        }""")
        with self.assertRaises(BrowserStepError):
            self.preparer.fill_metadata()
        self.assertIsNone(self.page.locator("#description").get_attribute("data-placeholder"))
        self.assertFalse(self.preparer.report["checks"].get("description", False))
        self.assert_not_published()

    def test_unchanged_old_editor_cover_is_rejected(self):
        self.upload()
        self.page.evaluate("state.staleImport = true")
        with self.assertRaises(BrowserStepError):
            self.preparer.set_cover()
        self.assertEqual(self.page.evaluate("state.coverFiles"), ["vertical.png"])
        self.assertFalse(self.preparer.report["checks"].get("cover", False))
        self.assert_not_published()

    def test_closing_cover_modal_without_updating_main_preview_is_rejected(self):
        self.upload()
        old_preview = self.page.locator("#cover-preview").evaluate("el => el.toDataURL()")
        self.page.evaluate("state.dropApply = true")
        with self.assertRaises(BrowserStepError):
            self.preparer.set_cover()
        self.assertFalse(self.page.locator("#cover-dialog").is_visible())
        self.assertEqual(self.page.locator("#cover-preview").evaluate("el => el.toDataURL()"), old_preview)
        self.assertFalse(self.preparer.report["checks"].get("cover", False))
        self.assert_not_published()

    def test_existing_authenticated_upload_page_is_used_without_navigation(self):
        for url in (UPLOAD_URL, "https://creator.rednote.com/publish/publish"):
            with self.subTest(url=url):
                self.page.goto(url)
                requests = []
                listener = lambda request: requests.append(request.url)
                self.page.on("request", listener)
                self.preparer.wait_for_upload_page()
                self.page.remove_listener("request", listener)
                self.assertEqual(requests, [])

    def test_login_route_with_hidden_video_input_is_not_authenticated(self):
        self.page.goto("https://creator.rednote.com/login")
        self.page.locator("#video").evaluate("el => el.hidden = true")
        with self.assertRaises(BrowserStepError) as caught:
            self.preparer.wait_for_upload_page()
        self.assertEqual(caught.exception.code, "login_or_upload_page_timeout")
        self.assertFalse(self.preparer.report["login_state_saved"])
        self.assertFalse((self.root / "output" / "manual-handoff.json").exists())

    def test_existing_home_or_manage_navigates_once_to_upload_page(self):
        for path in ("/home", "/manage"):
            with self.subTest(path=path):
                self.page.goto("https://creator.rednote.com" + path)
                requests = []
                listener = lambda request: requests.append(request.url)
                self.page.on("request", listener)
                self.preparer.wait_for_upload_page()
                self.page.remove_listener("request", listener)
                self.assertEqual(requests, [UPLOAD_URL])

    def test_official_sso_recovers_after_intermediate_401_without_early_prompt(self):
        self.context.unroute("**/*")
        visits = []
        fixture = FIXTURE.read_text(encoding="utf-8")

        def official_flow(route):
            path = urlsplit(route.request.url).path
            visits.append(path)
            if path == "/publish/publish" and visits.count(path) == 1:
                # A browser routing handler can allow server redirect chains
                # past the handler. Use the SPA's client navigation so every
                # individual fixture request remains intercepted.
                route.fulfill(content_type="text/html", body="<script>location.href='/login'</script>")
            elif path == "/login":
                route.fulfill(content_type="text/html", body="""扫码登录
                    <input type=file accept='video/mp4' hidden>
                    <script>fetch('/official-auto-login').then(() =>
                        setTimeout(() => location.href='/publish/publish', 150));</script>""")
            elif path == "/official-auto-login":
                route.fulfill(status=401, content_type="application/json", body='{"success":false}')
            else:
                route.fulfill(content_type="text/html", body=fixture)

        self.context.route("**/*", official_flow)
        self.preparer.timeout_seconds = 3
        self.preparer.config["auth_restore_grace_seconds"] = 2
        output = io.StringIO()
        with redirect_stdout(output):
            try:
                self.preparer.wait_for_upload_page()
            except BrowserStepError:
                self.fail(f"Offline SSO did not finish: {visits!r}, {self.page.url}")
        self.assertIn("/official-auto-login", visits)
        self.assertEqual(visits.count("/publish/publish"), 2)
        self.assertEqual(visits.count("/login"), 1)
        self.assertNotIn("完成小红书登录", output.getvalue())
        self.assertTrue(self.page.url.endswith("/publish/publish"))
        self.assertEqual(self.page.evaluate("state.videoFiles"), [])

    def test_verified_upload_form_saves_private_state_and_reports_only_summary(self):
        profile = self.root / "dedicated-profile"
        profile.mkdir()
        self.preparer.config["browser_profile"] = str(profile)
        self.preparer.profile_dir = profile
        self.preparer._session_backup_enabled = True
        self.page.goto("https://creator.rednote.com/publish/publish")
        self.context.add_cookies([{"name": "fixture_auth", "value": "synthetic-private-value", "domain": ".rednote.com", "path": "/"}])
        self.preparer.wait_for_upload_page()
        self.assertTrue(self.preparer.report["login_state_saved"])
        self.assertTrue((profile / SESSION_FILENAME).is_file())
        self.assertNotIn("synthetic-private-value", json.dumps(self.preparer.report))
        self.assertFalse((self.root / "output" / SESSION_FILENAME).exists())

    def test_failed_private_save_does_not_claim_cookie_persistence(self):
        self.page.goto(UPLOAD_URL)
        self.preparer._session_backup_enabled = True
        with patch("xhs_publisher.browser.save_authenticated_session", side_effect=SessionStateError("private save failed")):
            self.preparer.wait_for_upload_page()
        self.assertFalse(self.preparer.report["login_state_saved"])
        self.assertIn("SessionStateError", self.preparer.report["warnings"][-1])

    def test_session_save_after_user_closed_tab_is_skipped(self):
        self.page.goto(UPLOAD_URL)
        self.page.close()
        self.assertFalse(self.preparer.save_verified_session())

    def test_css_background_cover_preview_is_observable(self):
        import base64
        self.page.goto(UPLOAD_URL)
        uri = "data:image/png;base64," + base64.b64encode(Path(self.manifest["cover"]).read_bytes()).decode()
        self.page.evaluate("""uri => {
            const div=document.createElement('div'); div.id='background-preview';
            div.style.cssText='width:90px;height:120px;background-size:100% 100%;';
            div.style.backgroundImage='url("'+uri+'")'; document.body.append(div);
        }""", uri)
        state = self.preparer._image_state(self.page, "#background-preview")
        self.assertEqual(state["kind"], "background")
        self.assertTrue(state["pixel_sha256"])

    def test_preflight_rechecks_fields_changed_after_successful_preparation(self):
        self.preparer.prepare()
        self.page.locator("#title").fill("页面被改成另一标题")
        with self.assertRaises(BrowserStepError):
            self.preparer.preflight()
        self.assertFalse(self.preparer.report["checks"].get("title", False))
        self.assert_not_published()

    def test_different_new_artwork_is_rejected_even_if_main_preview_changes(self):
        self.upload()
        self.page.evaluate("""() => {
            const button=document.getElementById('apply-cover'), previous=button.onclick;
            button.onclick=() => {
                previous();
                const c=document.getElementById('cover-preview'), ctx=c.getContext('2d');
                ctx.fillStyle='#aa22dd'; ctx.fillRect(0,0,c.width,c.height);
            };
        }""")
        with self.assertRaises(BrowserStepError):
            self.preparer.set_cover()
        self.assertFalse(self.preparer.report["checks"].get("cover", False))
        self.assert_not_published()

    def test_cover_replaced_since_manifest_load_is_rejected_before_upload(self):
        self.upload()
        Image.new("RGB", (120, 160), "yellow").save(self.manifest["cover"])
        with self.assertRaises(BrowserStepError):
            self.preparer.set_cover()
        self.assertEqual(self.page.evaluate("state.coverFiles"), [])
        self.assert_not_published()

    def test_cover_input_can_clear_itself_after_change(self):
        self.upload()
        self.page.locator("#cover-file").evaluate("""el => {
            el.addEventListener('change', () => { el.value=''; }, {once:true});
        }""")
        self.preparer.set_cover()
        self.assertTrue(self.preparer.report["checks"]["cover"])
        self.assertEqual(self.preparer.report["cover_evidence"]["selected_file"]["name"], "vertical.png")
        self.assertEqual(self.page.locator("#cover-file").evaluate("el=>el.files.length"), 0)
        self.assert_not_published()

    def test_cropper_artwork_read_ignores_ui_overlay_but_preserves_actual_crop(self):
        self.page.goto(UPLOAD_URL)
        uri = "data:image/png;base64," + base64.b64encode(Path(self.manifest["cover"]).read_bytes()).decode()
        self.page.evaluate("""uri => {
            const box=document.createElement('div'); box.id='cropper';
            box.className='cropper-view-box';
            box.style.cssText='position:relative;width:90px;height:120px;overflow:hidden;';
            const image=new Image();image.src=uri;
            image.style.cssText='display:block;width:90px;height:120px;';
            const overlay=document.createElement('div');
            overlay.style.cssText='position:absolute;inset:0;background:rgba(255,255,255,.1);';
            box.append(image,overlay);document.body.append(box);
        }""", uri)
        image = self.page.locator("#cropper img")
        self.page.wait_for_function("document.querySelector('#cropper img').naturalWidth === 120")
        reference = self.manifest["cover"]
        self.assertFalse(compare_cover(reference, self.page.locator("#cropper").screenshot())["matches"])
        self.assertTrue(compare_cover(reference, self.preparer._preview_png(image))["matches"])
        image.evaluate("""el => {
            el.style.cssText='position:absolute;width:144px;height:192px;left:-27px;top:-60px;';
        }""")
        self.assertFalse(compare_cover(reference, self.preparer._preview_png(image))["matches"])
        self.assert_not_published()

    def test_same_correct_cover_can_be_applied_again_without_content_change(self):
        self.upload()
        self.preparer.set_cover()
        self.preparer.set_cover()
        evidence = self.preparer.report["cover_evidence"]
        self.assertEqual(evidence["selected_file"]["sha256"], self.manifest["cover_source"]["sha256"])
        for stage in ("imported", "applied"):
            self.assertIs(evidence[stage]["content_changed"], False)
            self.assertIs(evidence[stage]["artwork_comparison"]["matches"], True)
        self.assertTrue(self.preparer.report["checks"]["cover"])
        self.assertEqual(self.page.evaluate("state.coverFiles"), ["vertical.png", "vertical.png"])
        self.assert_not_published()

    def test_closed_publish_component_is_left_for_user_review_without_clicking(self):
        self.page.goto(UPLOAD_URL)
        self.page.evaluate("""() => {
            customElements.define('xhs-publish-btn', class extends HTMLElement {
                connectedCallback() {
                    const root=this.attachShadow({mode:'closed'}), button=document.createElement('button');
                    button.textContent='发布'; button.disabled=true;
                    button.onclick=() => { state.published += 1; };
                    root.append(button);
                    this.style.cssText='display:inline-block;width:100px;height:40px;';
                    this.addEventListener('click', () => { state.published += 1; });
                }
            });
            document.getElementById('publish').replaceWith(document.createElement('xhs-publish-btn'));
        }""")
        self.preparer.config["publish_button"] = "xhs-publish-btn"
        report = self.preparer.prepare()
        self.assertIsNone(self.page.locator("xhs-publish-btn").evaluate("el => el.shadowRoot"))
        self.assertEqual(report["final_control"], "closed_component_visible_for_user_review")
        self.assertEqual(report["status"], "awaiting_user_publish")
        self.assert_not_published()


if __name__ == "__main__":
    unittest.main()
