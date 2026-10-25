"""Offline creator-page integration tests. All creator URLs are intercepted."""

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from douyin_agent.browser import BrowserStepError, _Publisher, _save_report, rank_collections, run_publish

FIXTURE = Path(__file__).parent / "fixtures" / "creator.html"


class CollectionTests(unittest.TestCase):
    def test_saved_preview_report_omits_private_url_queries(self):
        report = {"source": {"sha256": "kept"}, "cover_evidence": {
            "horizontal": {"applied_after": {"source": "https://private.example/cover.jpg?policy=private&sign=private"}},
        }}
        with tempfile.TemporaryDirectory() as folder:
            _save_report(report, Path(folder))
            saved = json.loads((Path(folder) / "browser-report.json").read_text())
        self.assertEqual(saved["cover_evidence"]["horizontal"]["applied_after"]["source"], "https://private.example/cover.jpg")
        self.assertEqual(saved["source"], {"sha256": "kept"})

    def test_visible_heat_only_ranks_matching_options(self):
        options = [
            {"name": "做饭", "text": "做饭\n热度：999万"},
            {"name": "示例教学", "text": "示例教学\n播放量：1.2万"},
            {"name": "示例日常", "text": "示例日常\n播放量：8.3万"},
        ]
        selected, warnings = rank_collections(options, ["示例", "示例教学", "示例"])
        self.assertEqual(selected["name"], "示例日常")
        self.assertTrue(any("近期热度" in item for item in warnings))

    def test_absent_preferred_is_not_replaced_with_another_collection(self):
        selected, warnings = rank_collections(
            [{"name": "示例", "text": "示例"}], ["示例"], preferred="我的示例教学",
        )
        self.assertIsNone(selected)
        self.assertTrue(warnings)

    def test_collection_episode_count_does_not_claim_popularity(self):
        selected, warnings = rank_collections(
            [{"name": "示例教学", "text": "示例教学\n200集"}], ["示例"],
        )
        self.assertEqual(selected["name"], "示例教学")
        self.assertTrue(any("未显示可比较" in item for item in warnings))


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
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"offline fixture video; never sent over a network")
        Image.new("RGB", (120, 160), "#cc3322").save(self.root / "vertical.jpg")
        Image.new("RGB", (160, 120), "#228844").save(self.root / "horizontal.jpg")
        self.manifest = {
            "job_id": "fixture", "video": str(self.video), "title": "学习像旅行：认真思考",
            "description": "先理解内容，再练习。", "hashtags": ["示例", "示例教学"],
            "collection": {"keywords": ["示例", "示例教学", "示例"], "required": True},
            "declaration": None,
            "covers": {"vertical": str(self.root / "vertical.jpg"), "horizontal": str(self.root / "horizontal.jpg")},
            "output_dir": str(self.root / "output"),
        }
        self.context = self.browser.new_context()
        self.context.route("**/*", lambda route: route.fulfill(body=FIXTURE.read_text(encoding="utf-8"), content_type="text/html"))
        self.page = self.context.new_page()
        self.page.set_default_timeout(2000)
        self.publisher = _Publisher(self.page, self.manifest, {
            "review": False, "cover_imported": "#imported-preview",
            "cover_vertical_ready": "#vertical-preview", "cover_horizontal_ready": "#horizontal-preview",
        }, timeout_seconds=3)

    def tearDown(self):
        self.context.close()
        self.folder.cleanup()

    def test_authenticated_page_delayed_upload_input_does_not_prompt_for_login(self):
        self.page.route("**/*", lambda route: route.fulfill(
            body='''<!doctype html><meta charset="utf-8"><p>创作者工作台正在加载</p>
                <script>setTimeout(() => {
                    const input = document.createElement('input');
                    input.type = 'file';
                    input.accept = 'video/*';
                    document.body.append(input);
                }, 950);</script>''',
            content_type="text/html",
        ))
        with patch("builtins.print") as output:
            video_input = self.publisher.wait_for_upload_page()
        self.assertEqual(video_input.get_attribute("accept"), "video/*")
        output.assert_not_called()

    def test_explicit_login_or_verification_page_prompts_once_and_still_requires_upload_input(self):
        self.publisher.timeout_seconds = 1
        for notice in ("请使用抖音扫码登录", "请完成安全验证"):
            with self.subTest(notice=notice):
                self.page.route("**/*", lambda route: route.fulfill(
                    body=f'<!doctype html><meta charset="utf-8"><p>{notice}</p>', content_type="text/html",
                ))
                with patch("builtins.print") as output:
                    with self.assertRaises(BrowserStepError) as caught:
                        self.publisher.wait_for_upload_page()
                self.assertEqual(caught.exception.code, "login_or_upload_page_timeout")
                self.assertIn(notice, self.publisher._body_text())
                output.assert_called_once()
                self.assertIn("完成页面要求的登录或验证", output.call_args.args[0])

    def test_long_page_load_emits_waiting_message_without_requesting_login(self):
        self.page.route("**/*", lambda route: route.fulfill(
            body='<!doctype html><meta charset="utf-8"><p>创作者工作台正在加载</p>', content_type="text/html",
        ))
        self.publisher.timeout_seconds = 30
        clock = SimpleNamespace(monotonic=unittest.mock.Mock(side_effect=[0, 11, 22, 33]))
        with patch("douyin_agent.browser.time", clock), patch("builtins.print") as output:
            with self.assertRaises(BrowserStepError) as caught:
                self.publisher.wait_for_upload_page()
        self.assertEqual(caught.exception.code, "login_or_upload_page_timeout")
        output.assert_called_once()
        self.assertIn("等待抖音上传页面加载", output.call_args.args[0])
        self.assertNotIn("登录", output.call_args.args[0])
        self.assertNotIn("扫码", output.call_args.args[0])

    def test_fill_distinguishes_three_file_inputs_and_never_publishes(self):
        report = self.publisher.run(publish=False)
        state = self.page.evaluate("window.state")
        self.assertEqual(report["status"], "filled")
        self.assertEqual(state["published"], 0)
        self.assertEqual(state["covers"], {"vertical": "vertical.jpg", "horizontal": "horizontal.jpg"})
        self.assertEqual(state["collection"], "示例教学")
        self.assertEqual(self.page.locator("#unrelated").evaluate("el => el.files.length"), 0)
        self.assertEqual(self.page.locator("#description").inner_text(),
                         "先理解内容，再练习。 #示例 #示例教学")
        self.assertFalse((self.root / "output" / "publish-attempt.json").exists())
        self.assertTrue(Path(report["screenshot"]).is_file())

    def _two_step_editor(self, *, fail_landscape=False):
        fixture = FIXTURE.read_text(encoding="utf-8").replace(
            '<button id="apply-cover" disabled>完成</button>',
            '<button id="next-cover" hidden>设置横封面</button><button id="apply-cover" disabled>完成</button>',
        )
        fixture = fixture.replace('</script>', '''
const next = el('next-cover');
state.advanced = 0;
state.applied = 0;
document.querySelectorAll('.cover').forEach(button => button.onclick = () => {
  state.editing = button.id;
  el('cover-file').value = '';
  el('cover-file').hidden = true;
  el('apply-cover').disabled = true;
  next.hidden = state.editing !== 'vertical';
  el('cover-dialog').hidden = false;
});
const singleLoad = window.loadCoverPreview;
window.loadCoverPreview = async file => {
  if (window.failLandscape && state.editing === 'horizontal') return;
  await singleLoad(file);
  if (state.editing === 'vertical') el('apply-cover').disabled = true;
};
next.onclick = () => {
  state.advanced += 1;
  state.portrait = {name:el('cover-file').files[0].name, src:state.importedDataURL};
  state.editing = 'horizontal';
  el('cover-file').value = '';
  el('imported-preview').hidden = true;
  el('apply-cover').disabled = true;
  next.hidden = true;
};
el('apply-cover').onclick = () => {
  state.applied += 1;
  for (const [kind, source] of Object.entries({vertical:state.portrait,
      horizontal:{name:el('cover-file').files[0].name,src:state.importedDataURL}})) {
    state.covers[kind] = source.name;
    el(kind).textContent = '更换封面';
    const img = document.createElement('img'); img.src = source.src;
    el(kind + '-preview').replaceChildren(img);
  }
  el('cover-dialog').hidden = true;
};
</script>''')
        if fail_landscape:
            fixture = fixture.replace('<script>', '<script>window.failLandscape = true;')
        self.page.route("**/*", lambda route: route.fulfill(body=fixture, content_type="text/html"))

    def test_two_step_editor_uploads_both_then_applies_once_without_publishing(self):
        self._two_step_editor()
        report = self.publisher.run(publish=False)
        state = self.page.evaluate("window.state")
        self.assertEqual(state["advanced"], 1)
        self.assertEqual(state["applied"], 1)
        self.assertEqual(state["covers"], {"vertical": "vertical.jpg", "horizontal": "horizontal.jpg"})
        self.assertEqual(state["published"], 0)
        self.assertTrue(report["checks"]["cover_vertical"])
        self.assertTrue(report["checks"]["cover_horizontal"])
        self.assertEqual(report["cover_evidence"]["vertical"]["editor_flow"], "portrait_then_landscape")

    def test_two_step_failed_landscape_never_saves_or_marks_portrait_complete(self):
        self._two_step_editor(fail_landscape=True)
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.run(publish=False)
        self.assertEqual(caught.exception.code, "cover_not_confirmed")
        state = self.page.evaluate("window.state")
        self.assertEqual(state["advanced"], 1)
        self.assertEqual(state["applied"], 0)
        self.assertEqual(state["published"], 0)
        self.assertFalse(self.publisher.report["checks"].get("cover_vertical"))
        self.assertFalse(self.publisher.report["checks"].get("cover_horizontal"))

    def test_two_step_waits_past_temporary_portrait_in_landscape_card(self):
        self._two_step_editor()
        # Replace the last script ending, after the two-step fixture handlers.
        self.page.add_init_script('''window.addEventListener('DOMContentLoaded', () => {
          const apply = document.getElementById('apply-cover');
          const original = apply.onclick;
          apply.onclick = () => {
            original();
            const landscape = document.querySelector('#horizontal-preview img');
            const finalSource = landscape.src;
            landscape.src = state.portrait.src;
            setTimeout(() => landscape.src = finalSource, 700);
          };
        });''')
        report = self.publisher.run(publish=False)
        final = report["cover_evidence"]["horizontal"]["applied_after"]
        self.assertAlmostEqual(final["width"] / final["height"], 4 / 3)
        self.assertNotEqual(final["source"], report["cover_evidence"]["vertical"]["applied_after"]["source"])

    def test_cover_file_identity_survives_synchronous_input_reset(self):
        self.publisher.wait_for_upload_page().set_input_files(str(self.video))
        self.page.evaluate('''() => {
          const input = document.getElementById('cover-file');
          const oldLoad = window.loadCoverPreview;
          window.loadCoverPreview = async file => {
            input.value = '';
            await oldLoad(file);
          };
          document.getElementById('apply-cover').onclick = () => {
            const img = document.createElement('img'); img.src = state.importedDataURL;
            document.getElementById(state.editing + '-preview').replaceChildren(img);
            document.getElementById(state.editing).textContent = '更换封面';
            document.getElementById('cover-dialog').hidden = true;
          };
        }''')
        self.publisher.set_cover("vertical")
        evidence = self.publisher.report["cover_evidence"]["vertical"]
        self.assertEqual(evidence["selected_file"]["sha256"], hashlib.sha256((self.root / "vertical.jpg").read_bytes()).hexdigest())
        self.assertEqual(self.page.locator("#cover-file").evaluate("el => el.files.length"), 0)
        self.assertTrue(self.publisher.report["checks"]["cover_vertical"])

    def test_description_is_verified_when_input_removes_placeholder(self):
        expected = "先理解内容，再练习。 #示例 #示例教学"
        for kind in ("contenteditable", "textarea"):
            with self.subTest(kind=kind):
                self.publisher.wait_for_upload_page().set_input_files(str(self.video))
                self.page.locator("#description").evaluate("""(el, kind) => {
                    if (kind === 'textarea') {
                        const textarea = document.createElement('textarea');
                        textarea.id = el.id;
                        textarea.placeholder = el.dataset.placeholder;
                        el.replaceWith(textarea);
                        el = textarea;
                    }
                    el.addEventListener('input', () => {
                        el.removeAttribute('data-placeholder');
                        el.removeAttribute('placeholder');
                    });
                }""", kind)
                self.publisher.fill_metadata()
                editor = self.page.locator("#description")
                self.assertIsNone(editor.get_attribute("data-placeholder"))
                self.assertIsNone(editor.get_attribute("placeholder"))
                actual = editor.input_value() if kind == "textarea" else editor.inner_text()
                self.assertEqual(actual, expected)
                self.assertTrue(self.publisher.report["checks"]["description"])
                self.assertEqual(self.page.evaluate("state.published"), 0)

    def test_description_mismatch_after_placeholder_removal_is_rejected(self):
        self.publisher.wait_for_upload_page().set_input_files(str(self.video))
        self.page.locator("#description").evaluate("""el => {
            el.addEventListener('input', () => {
                el.removeAttribute('data-placeholder');
                el.textContent = '页面修改后的不同简介';
            });
        }""")
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.fill_metadata()
        self.assertEqual(caught.exception.code, "description_not_confirmed")
        self.assertFalse(self.publisher.report["checks"].get("description", False))
        self.assertEqual(self.page.evaluate("state.published"), 0)

    def test_description_ignores_only_slate_empty_enter_marker(self):
        self.manifest["description"] = "一起练习 👨‍👩‍👧‍👦 和 👩🏽‍💻"
        expected = self.manifest["description"] + " #示例 #示例教学"
        self.publisher.wait_for_upload_page().set_input_files(str(self.video))
        self.page.locator("#description").evaluate("""el => {
            el.setAttribute('data-slate-editor', 'true');
            el.addEventListener('input', () => {
                const value = el.textContent;
                el.removeAttribute('data-placeholder');
                el.innerHTML = '<div class="ace-line" data-node="true"><div data-line-wrapper="true" dir="auto">'
                    + '<span data-leaf="true"><span data-string="true"></span></span>'
                    + '<span data-leaf="true"><span data-string="true" data-enter="true"></span></span>'
                    + '</div></div>';
                el.querySelector('[data-string]').textContent = value;
                el.querySelector('[data-enter]').textContent = '\\u200b';
            });
        }""")
        self.publisher.fill_metadata()
        self.assertTrue(self.publisher.report["checks"]["description"])
        self.assertEqual(self.page.locator("#description").inner_text(), expected + "\u200b")
        self.assertEqual(self.page.locator('#description [data-enter="true"]').text_content(), "\u200b")
        self.assertEqual(self.page.evaluate("state.published"), 0)

    def test_slate_marker_handling_preserves_substantive_character_checks(self):
        self.manifest["description"] = "一起练习 👩🏽‍💻"
        for mutation in ("body_zero_width_space", "body_missing_emoji_joiner", "nonempty_marker"):
            with self.subTest(mutation=mutation):
                self.publisher.wait_for_upload_page().set_input_files(str(self.video))
                self.page.locator("#description").evaluate("""(el, mutation) => {
                    el.setAttribute('data-slate-editor', 'true');
                    el.addEventListener('input', () => {
                        const value = el.textContent;
                        el.removeAttribute('data-placeholder');
                        const text = document.createElement('span');
                        text.setAttribute('data-string', 'true');
                        text.textContent = mutation === 'body_zero_width_space' ? value + '\\u200b'
                            : mutation === 'body_missing_emoji_joiner' ? value.replace('\\u200d', '') : value;
                        const marker = document.createElement('span');
                        marker.setAttribute('data-string', 'true');
                        marker.setAttribute('data-enter', 'true');
                        marker.textContent = mutation === 'nonempty_marker' ? '\\u200b不同文字' : '\\u200b';
                        el.replaceChildren(text, marker);
                    });
                }""", mutation)
                with self.assertRaises(BrowserStepError) as caught:
                    self.publisher.fill_metadata()
                self.assertEqual(caught.exception.code, "description_not_confirmed")
                self.assertFalse(self.publisher.report["checks"].get("description", False))
                self.assertEqual(self.page.evaluate("state.published"), 0)

    def test_duplicate_description_editors_remain_ambiguous(self):
        self.publisher.wait_for_upload_page().set_input_files(str(self.video))
        self.page.locator("#description").evaluate("""el => {
            const duplicate = el.cloneNode(true);
            duplicate.id = 'description-duplicate';
            el.after(duplicate);
        }""")
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.fill_metadata()
        self.assertEqual(caught.exception.code, "ambiguous_control")
        self.assertEqual(self.page.locator("#description").inner_text(), "")
        self.assertEqual(self.page.evaluate("state.published"), 0)

    def test_replaced_description_editor_is_not_verified_using_detached_content(self):
        self.publisher.wait_for_upload_page().set_input_files(str(self.video))
        self.page.locator("#description").evaluate("""el => {
            el.addEventListener('input', () => {
                const replacement = el.cloneNode(true);
                replacement.removeAttribute('data-placeholder');
                replacement.textContent = '替换后的不同简介';
                el.replaceWith(replacement);
            });
        }""")
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.fill_metadata()
        self.assertEqual(caught.exception.code, "description_not_confirmed")
        self.assertFalse(self.publisher.report["checks"].get("description", False))
        self.assertEqual(self.page.locator("#description").inner_text(), "替换后的不同简介")
        self.assertEqual(self.page.evaluate("state.published"), 0)

    def test_explicit_publish_requires_success_evidence_and_records_attempt(self):
        report = self.publisher.run(publish=True)
        self.assertEqual(report["status"], "published")
        self.assertEqual(self.page.evaluate("state.published"), 1)
        attempt = json.loads((self.root / "output" / "publish-attempt.json").read_text())
        self.assertEqual(attempt["job_id"], "fixture")
        self.assertEqual(attempt["state"], "publish_attempted")

    def test_click_without_success_retains_attempt_and_prevents_retry(self):
        self.publisher.config["success_timeout_seconds"] = 0
        self.publisher.run(publish=False)
        self.page.evaluate("window.suppressSuccess = true")
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.submit()
        self.assertEqual(caught.exception.code, "publish_result_unknown")
        self.assertEqual(self.publisher.report["status"], "publish_attempted")
        self.assertEqual(self.page.evaluate("state.published"), 1)
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.run(publish=True)
        self.assertEqual(caught.exception.code, "duplicate_publish_attempt")
        self.assertEqual(self.page.evaluate("state.published"), 1)

    def test_static_publish_help_is_not_publish_success_evidence(self):
        self.publisher.run(publish=False)
        self.publisher.config["success_timeout_seconds"] = 1
        self.page.evaluate("""() => {
            window.suppressSuccess = true;
            const help = document.createElement('p');
            help.textContent = '点击发布后，如作品还在上传中，请勿关闭页面，等待上传发布完成。';
            document.body.append(help);
        }""")
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.submit()
        self.assertEqual(caught.exception.code, "publish_result_unknown")
        self.assertEqual(self.publisher.report["status"], "publish_attempted")
        self.assertEqual(self.page.evaluate("state.published"), 1)
        self.assertTrue((self.root / "output" / "publish-attempt.json").exists())

    def _publication_list_on_submit(self, works, *, management_url=True):
        self.page.evaluate("""({works, managementUrl}) => {
            document.getElementById('publish').onclick = () => {
                state.published++;
                if (managementUrl) history.pushState({}, '', '/creator-micro/content/manage');
                document.body.replaceChildren();
                for (const work of works) {
                    const card = document.createElement('div');
                    card.className = 'video-card-content-Xt386b';
                    const info = document.createElement('div');
                    info.className = 'video-card-info-dzTKoo';
                    for (const [cls, text] of [
                        ['info-title-text-Zob9RV', work.caption],
                        ['info-status-QukAhD', work.status],
                        ['info-time-tP3i_r', '2026年10月18日 19:15']
                    ]) {
                        const field = document.createElement('div');
                        field.className = cls;
                        field.textContent = text;
                        info.append(field);
                    }
                    card.append(info);
                    document.body.append(card);
                }
            };
        }""", {"works": works, "managementUrl": management_url})

    def test_unique_target_publication_is_confirmed_with_its_own_review_status(self):
        for status, platform_status in (("审核中", "under_review"), ("已发布", "published")):
            with self.subTest(status=status):
                # Each simulated submission uses its own durable sentinel.
                self.publisher.output_dir = self.root / status
                self.publisher.output_dir.mkdir()
                self.publisher.run(publish=False)
                self._publication_list_on_submit([
                    {"caption": "旧作品 与本条无关", "status": "已发布"},
                    {"caption": "另一条旧作品", "status": "审核未通过"},
                    {"caption": self.manifest["title"] + " 先理解内容，再练习。 #示例 #示例教学", "status": status},
                ])
                self.publisher.submit()
                self.assertEqual(self.publisher.report["status"], "published")
                self.assertEqual(self.publisher.report["platform_status"], platform_status)
                self.assertEqual(self.publisher.report["publication"]["status_text"], status)
                self.assertEqual(self.publisher.report["publication"]["published_at_text"], "2026年10月18日 19:15")
                self.assertTrue(Path(self.publisher.report["screenshot"]).is_file())

    def test_other_published_works_and_same_title_with_different_description_do_not_confirm_publish(self):
        self.publisher.run(publish=False)
        self.publisher.config["success_timeout_seconds"] = 1
        self._publication_list_on_submit([
            {"caption": "旧作品 与本条无关", "status": "已发布"},
            {"caption": self.manifest["title"] + " 另一条不同的简介", "status": "已发布"},
        ])
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.submit()
        self.assertEqual(caught.exception.code, "publish_result_unknown")
        self.assertEqual(self.publisher.report["status"], "publish_attempted")

    def test_publication_card_on_upload_page_is_not_list_evidence(self):
        self.publisher.run(publish=False)
        self.publisher.config["success_timeout_seconds"] = 1
        self._publication_list_on_submit([
            {"caption": self.manifest["title"] + " 先理解内容，再练习。 #示例 #示例教学", "status": "审核中"},
        ], management_url=False)
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.submit()
        self.assertEqual(caught.exception.code, "publish_result_unknown")
        self.assertEqual(self.publisher.report["status"], "publish_attempted")

    def test_duplicate_exact_publication_cards_do_not_confirm_publish(self):
        self.publisher.run(publish=False)
        target = {"caption": self.manifest["title"] + " 先理解内容，再练习。 #示例 #示例教学", "status": "审核中"}
        self._publication_list_on_submit([target, target])
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.submit()
        self.assertEqual(caught.exception.code, "ambiguous_published_work")
        self.assertEqual(self.publisher.report["status"], "publish_attempted")

    def test_required_collection_absent_blocks_before_publish(self):
        self.manifest["collection"]["preferred"] = "不存在的示例合集"
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.run(publish=True)
        self.assertEqual(caught.exception.code, "required_collection_missing")
        self.assertEqual(self.page.evaluate("state.published"), 0)
        self.assertFalse((self.root / "output" / "publish-attempt.json").exists())

    def test_optional_absent_collection_keeps_placeholder_and_allows_fill(self):
        self.manifest["collection"].update(preferred="不存在的示例合集", required=False)
        report = self.publisher.run(publish=False)
        self.assertEqual(report["status"], "filled")
        self.assertIsNone(report["collection"])
        self.assertEqual(self.page.locator("#collection").inner_text(), "请选择合集")
        self.assertEqual(self.page.evaluate("state.published"), 0)

    def test_missing_cover_confirmation_blocks_before_publish(self):
        self.page.route("**/*", lambda route: route.fulfill(
            body=FIXTURE.read_text(encoding="utf-8").replace('role="dialog" id="cover-dialog"', 'id="cover-dialog"'),
            content_type="text/html",
        ))
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.run(publish=True)
        # Without a confirmed dialog, neither image input may receive a cover.
        self.assertEqual(caught.exception.code, "file_input_not_identified")
        self.assertEqual(self.page.evaluate("state.published"), 0)
        self.assertEqual(self.page.locator('#unrelated').evaluate("el => el.files.length"), 0)
        self.assertEqual(self.page.locator('#cover-file').evaluate("el => el.files.length"), 0)

    def _prepare_multi_input_cover_editor(self):
        self.publisher.wait_for_upload_page().set_input_files(str(self.video))
        self.page.evaluate("""() => {
            const dialog = document.getElementById('cover-dialog');
            dialog.innerHTML = `
                <section class="list-Ldrppp"><h3>生成参考图</h3><div class="semi-upload">
                    <span>上传生成参考图</span>
                    <input id="reference-file" type="file" accept="image/*" class="semi-upload-hidden-input">
                    <input id="reference-replace" type="file" accept="image/*" class="semi-upload-hidden-input-replace" hidden>
                </div></section>
                <div id="local-cover-dropzone" class="semi-upload upload-BvM5FF"
                    style="position:relative;width:350px;height:140px">
                    <div id="local-tab">上传封面</div>
                    <div>点击上传文件或拖拽文件到这里</div>
                    <input id="cover-file" type="file" accept="image/*" class="semi-upload-hidden-input"
                        style="position:absolute;inset:0;width:100%;height:100%;opacity:0;z-index:2">
                    <input id="cover-replace" type="file" accept="image/*" class="semi-upload-hidden-input-replace" hidden>
                </div><canvas id="imported-preview" hidden></canvas><button id="apply-cover" disabled>完成</button>`;
            state.localTabClicks = 0;
            state.fileTargets = [];
            document.getElementById('local-tab').onclick = () => state.localTabClicks++;
            dialog.querySelectorAll('input[type="file"]').forEach(input => input.onchange = () => {
                state.fileTargets.push(input.id);
                if (input.id === 'cover-file') loadCoverPreview(input.files[0]);
            });
            document.querySelectorAll('.cover').forEach(button => button.onclick = () => {
                state.editing = button.id;
                document.getElementById('cover-file').value = '';
                document.getElementById('apply-cover').disabled = true;
                dialog.hidden = false;
            });
            document.getElementById('apply-cover').onclick = () => {
                const filename = document.getElementById('cover-file').files[0].name;
                state.covers[state.editing] = filename;
                document.getElementById(state.editing).textContent = '更换封面';
                const preview = document.createElement('img');
                preview.src = state.importedDataURL;
                document.getElementById(state.editing + '-preview').replaceChildren(preview);
                dialog.hidden = true;
            };
        }""")

    def test_cover_upload_uses_local_dropzone_input_without_clicking_covered_label(self):
        self._prepare_multi_input_cover_editor()
        file_choosers = []
        self.page.on("filechooser", lambda chooser: file_choosers.append(chooser))
        self.publisher.set_cover("vertical")
        self.publisher.set_cover("horizontal")
        state = self.page.evaluate("state")
        self.assertEqual(state["covers"], {"vertical": "vertical.jpg", "horizontal": "horizontal.jpg"})
        self.assertEqual(state["fileTargets"], ["cover-file", "cover-file"])
        self.assertEqual(state["localTabClicks"], 0)
        self.assertEqual(file_choosers, [])
        for name in ("reference-file", "reference-replace", "cover-replace"):
            self.assertEqual(self.page.locator(f"#{name}").evaluate("el => el.files.length"), 0)
        self.assertTrue(self.publisher.report["checks"]["cover_vertical"])
        self.assertTrue(self.publisher.report["checks"]["cover_horizontal"])
        self.assertEqual(state["published"], 0)

    def test_duplicate_local_cover_dropzones_remain_ambiguous(self):
        self._prepare_multi_input_cover_editor()
        self.page.locator('#local-cover-dropzone').evaluate("""el => {
            const duplicate = el.cloneNode(true);
            duplicate.removeAttribute('id');
            duplicate.querySelectorAll('[id]').forEach(node => node.removeAttribute('id'));
            el.after(duplicate);
        }""")
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.set_cover("vertical")
        self.assertEqual(caught.exception.code, "file_input_not_identified")
        self.assertEqual(self.page.evaluate("state.fileTargets"), [])
        self.assertEqual(self.page.evaluate("state.localTabClicks"), 0)
        self.assertEqual(self.page.evaluate("state.published"), 0)

    def test_cover_image_input_override_resolves_multi_input_editor(self):
        self._prepare_multi_input_cover_editor()
        self.page.locator('#local-cover-dropzone div').last.evaluate("el => { el.textContent = '新版上传区域'; }")
        self.publisher.config["cover_image_input"] = "#cover-file"
        self.publisher.set_cover("vertical")
        self.assertEqual(self.page.evaluate("state.fileTargets"), ["cover-file"])
        self.assertEqual(self.page.evaluate("state.localTabClicks"), 0)
        self.assertTrue(self.publisher.report["checks"]["cover_vertical"])

    def test_cover_waits_for_late_dialog_and_refreshes_scope(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                self._prepare_multi_input_cover_editor()
                if configured:
                    self.publisher.config.update(
                        cover_dialog="#cover-dialog",
                        cover_image_input=".semi-upload.upload-BvM5FF > input.semi-upload-hidden-input",
                    )
                self.page.evaluate("""() => {
                    const dialog = document.getElementById('cover-dialog');
                    dialog.remove();
                    document.querySelectorAll('.cover').forEach(button => button.onclick = () => {
                        state.editing = button.id;
                        setTimeout(() => {
                            document.body.append(dialog);
                            dialog.hidden = false;
                        }, 1000);
                    });
                }""")
                self.publisher.set_cover("vertical")
                self.assertEqual(self.page.evaluate("state.fileTargets"), ["cover-file"])
                self.assertEqual(self.page.locator('#unrelated').evaluate("el => el.files.length"), 0)
                self.assertTrue(self.publisher.report["checks"]["cover_vertical"])

    def test_cover_waits_for_late_upload_input_in_visible_dialog(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                self._prepare_multi_input_cover_editor()
                if configured:
                    self.publisher.config["cover_image_input"] = ".semi-upload.upload-BvM5FF > input.semi-upload-hidden-input"
                self.page.evaluate("""() => {
                    const input = document.getElementById('cover-file');
                    input.remove();
                    document.querySelectorAll('.cover').forEach(button => button.onclick = () => {
                        state.editing = button.id;
                        document.getElementById('cover-dialog').hidden = false;
                        setTimeout(() => document.getElementById('local-cover-dropzone').append(input), 1000);
                    });
                }""")
                self.publisher.set_cover("vertical")
                self.assertEqual(self.page.evaluate("state.fileTargets"), ["cover-file"])
                self.assertEqual(self.page.evaluate("state.localTabClicks"), 0)
                self.assertTrue(self.publisher.report["checks"]["cover_vertical"])

    def test_cover_duplicate_configured_upload_inputs_are_rejected_without_waiting(self):
        self._prepare_multi_input_cover_editor()
        self.publisher.config["cover_image_input"] = "input.semi-upload-hidden-input"
        with patch.object(self.page, "wait_for_timeout", wraps=self.page.wait_for_timeout) as waiting:
            with self.assertRaises(BrowserStepError) as caught:
                self.publisher.set_cover("vertical")
        self.assertEqual(caught.exception.code, "ambiguous_control")
        waiting.assert_not_called()
        self.assertEqual(self.page.evaluate("state.fileTargets"), [])

    def test_lone_reference_or_replacement_input_is_not_mistaken_for_cover_upload(self):
        for remaining in ("reference-file", "reference-replace"):
            with self.subTest(remaining=remaining):
                self._prepare_multi_input_cover_editor()
                self.page.evaluate("""remaining => {
                    document.getElementById('local-cover-dropzone').remove();
                    document.querySelectorAll('#cover-dialog input[type="file"]').forEach(input => {
                        if (input.id !== remaining) input.remove();
                    });
                    document.querySelectorAll('.cover').forEach(button => button.onclick = () => {
                        state.editing = button.id;
                        document.getElementById('cover-dialog').hidden = false;
                    });
                }""", remaining)
                with self.assertRaises(BrowserStepError) as caught:
                    self.publisher.set_cover("vertical")
                self.assertEqual(caught.exception.code, "file_input_not_identified")
                self.assertEqual(self.page.evaluate("state.fileTargets"), [])
                self.assertEqual(self.page.locator(f'#{remaining}').evaluate("el => el.files.length"), 0)

    def test_cover_local_tab_can_create_input_when_no_input_exists_yet(self):
        self.publisher.wait_for_upload_page().set_input_files(str(self.video))
        self.page.evaluate("""() => {
            document.getElementById('cover-file').remove();
            document.querySelectorAll('.cover').forEach(button => button.onclick = () => {
                state.editing = button.id;
                document.getElementById('cover-dialog').hidden = false;
            });
            document.getElementById('local-tab').onclick = () => {
                const input = document.createElement('input');
                input.id = 'cover-file';
                input.type = 'file';
                input.accept = 'image/*';
                input.onchange = () => loadCoverPreview(input.files[0]);
                document.getElementById('cover-dialog').append(input);
            };
        }""")
        self.publisher.set_cover("vertical")
        self.assertEqual(self.page.evaluate("state.covers.vertical"), "vertical.jpg")
        self.assertTrue(self.publisher.report["checks"]["cover_vertical"])

    def _seed_old_cover_previews(self):
        self.page.evaluate("""() => {
            const canvas = document.getElementById('imported-preview');
            canvas.width = 120;
            canvas.height = 160;
            canvas.getContext('2d').fillStyle = '#1133cc';
            canvas.getContext('2d').fillRect(0, 0, 120, 160);
            canvas.hidden = false;
            const image = document.createElement('img');
            image.src = canvas.toDataURL();
            document.getElementById('vertical-preview').replaceChildren(image);
        }""")

    def test_cover_waits_for_imported_pixels_with_already_enabled_done_button(self):
        self._prepare_multi_input_cover_editor()
        self._seed_old_cover_previews()
        self.page.evaluate("""() => {
            const button = document.getElementById('vertical');
            const open = button.onclick;
            button.onclick = () => { open(); document.getElementById('apply-cover').disabled = false; };
            document.getElementById('cover-file').onchange = event => {
                const file = event.target.files[0];
                setTimeout(async () => { await loadCoverPreview(file); state.importReady = true; }, 500);
            };
            const done = document.getElementById('apply-cover');
            const apply = done.onclick;
            done.onclick = () => { state.savedBeforeImport = !state.importReady; apply(); };
        }""")
        self.publisher.set_cover("vertical")
        self.assertFalse(self.page.evaluate("state.savedBeforeImport"))
        evidence = self.publisher.report["cover_evidence"]["vertical"]
        self.assertEqual(evidence["source_sha256"], hashlib.sha256((self.root / 'vertical.jpg').read_bytes()).hexdigest())
        self.assertEqual(evidence["selected_file"]["sha256"], evidence["source_sha256"])
        self.assertNotEqual(evidence["imported_before"]["pixel_sha256"], evidence["imported_after"]["pixel_sha256"])
        self.assertNotEqual(evidence["applied_before"]["pixel_sha256"], evidence["applied_after"]["pixel_sha256"])
        self.assertTrue(Path(evidence["applied_screenshot"]).is_file())
        self.assertTrue(self.publisher.report["checks"]["cover_vertical"])

    def test_old_editor_pixels_do_not_pass_when_done_button_is_enabled(self):
        self._prepare_multi_input_cover_editor()
        self._seed_old_cover_previews()
        self.publisher.config["cover_evidence_timeout_seconds"] = 1
        self.publisher.report["checks"]["cover_vertical"] = True
        self.page.evaluate("""() => {
            document.getElementById('cover-file').onchange = () => { document.getElementById('apply-cover').disabled = false; };
        }""")
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.set_cover("vertical")
        self.assertEqual(caught.exception.code, "cover_not_confirmed")
        self.assertFalse(self.publisher.report["checks"].get("cover_vertical", False))
        self.assertTrue(self.page.locator('#cover-dialog').is_visible())
        self.assertEqual(self.page.evaluate("state.covers"), {})
        self.assertTrue(Path(self.publisher.report["cover_evidence"]["vertical"]["error_screenshot"]).is_file())

    def test_closed_dialog_with_unchanged_old_thumbnail_does_not_confirm_cover(self):
        self._prepare_multi_input_cover_editor()
        self._seed_old_cover_previews()
        self.publisher.config["cover_evidence_timeout_seconds"] = 1
        self.page.evaluate("""() => {
            document.getElementById('apply-cover').onclick = () => { document.getElementById('cover-dialog').hidden = true; };
        }""")
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.set_cover("vertical")
        self.assertEqual(caught.exception.code, "cover_not_confirmed")
        self.assertFalse(self.publisher.report["checks"].get("cover_vertical", False))
        self.assertFalse(self.page.locator('#cover-dialog').is_visible())

    def test_missing_cover_evidence_config_clears_stale_check_and_saves_diagnostic(self):
        self.publisher.wait_for_upload_page().set_input_files(str(self.video))
        self.publisher.config.pop("cover_imported")
        self.publisher.report["checks"]["cover_vertical"] = True
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.set_cover("vertical")
        self.assertEqual(caught.exception.code, "cover_verification_not_configured")
        self.assertFalse(self.publisher.report["checks"].get("cover_vertical", False))
        evidence = self.publisher.report["cover_evidence"]["vertical"]
        self.assertIn("source_sha256", evidence)
        self.assertTrue(Path(evidence["error_screenshot"]).is_file())
        self.assertEqual(self.page.locator('#cover-file').evaluate("el => el.files.length"), 0)

    def test_cover_card_with_choose_cover_placeholder_cannot_confirm_applied_image(self):
        self._prepare_multi_input_cover_editor()
        self.publisher.config["cover_evidence_timeout_seconds"] = 1
        self.page.locator('#vertical').evaluate("""el => {
            el.parentElement.id = 'vertical-card';
            el.parentElement.className = 'coverControl-fixture';
        }""")
        self.publisher.config["cover_vertical_ready"] = "#vertical-card"
        self.page.evaluate("""() => {
            const done = document.getElementById('apply-cover');
            const apply = done.onclick;
            done.onclick = () => { apply(); document.getElementById('vertical').textContent = '选择封面'; };
        }""")
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.set_cover("vertical")
        self.assertEqual(caught.exception.code, "cover_not_confirmed")
        self.assertFalse(self.publisher.report["checks"].get("cover_vertical", False))

    def test_removing_placeholder_without_changing_old_artwork_does_not_confirm_cover(self):
        self._prepare_multi_input_cover_editor()
        self._seed_old_cover_previews()
        self.publisher.config["cover_evidence_timeout_seconds"] = 1
        self.page.locator('#vertical').evaluate("""el => {
            el.parentElement.id = 'vertical-card';
            el.parentElement.className = 'coverControl-fixture';
        }""")
        self.publisher.config["cover_vertical_ready"] = "#vertical-card"
        self.page.evaluate("""() => {
            document.getElementById('apply-cover').onclick = () => {
                document.getElementById('vertical').textContent = '更换封面';
                document.getElementById('cover-dialog').hidden = true;
            };
        }""")
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.set_cover("vertical")
        self.assertEqual(caught.exception.code, "cover_not_confirmed")
        self.assertFalse(self.publisher.report["checks"].get("cover_vertical", False))

    def test_new_thumbnail_url_with_identical_old_pixels_does_not_confirm_cover(self):
        self._prepare_multi_input_cover_editor()
        self._seed_old_cover_previews()
        self.publisher.config["cover_evidence_timeout_seconds"] = 1
        self.page.evaluate("""() => {
            document.getElementById('apply-cover').onclick = async () => {
                const image = document.querySelector('#vertical-preview img');
                image.src = URL.createObjectURL(await (await fetch(image.src)).blob());
                document.getElementById('vertical').textContent = '更换封面';
                document.getElementById('cover-dialog').hidden = true;
            };
        }""")
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.set_cover("vertical")
        self.assertEqual(caught.exception.code, "cover_not_confirmed")
        self.assertFalse(self.publisher.report["checks"].get("cover_vertical", False))

    def test_upload_input_value_is_not_imported_artwork_evidence(self):
        self._prepare_multi_input_cover_editor()
        self.publisher.config.update(cover_imported="#cover-file", cover_evidence_timeout_seconds=1)
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.set_cover("vertical")
        self.assertEqual(caught.exception.code, "cover_not_confirmed")
        self.assertFalse(self.publisher.report["checks"].get("cover_vertical", False))
        self.assertTrue(self.page.locator('#cover-dialog').is_visible())

    def test_unprocessed_upload_cannot_publish(self):
        self.publisher.config.update(upload_ready="#never-ready", upload_timeout_seconds=0)
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.run(publish=True)
        self.assertEqual(caught.exception.code, "video_processing_timeout")
        self.assertEqual(self.page.evaluate("state.published"), 0)
        self.assertFalse((self.root / "output" / "publish-attempt.json").exists())

    def test_confirmed_upload_ignores_static_upload_help_and_unrelated_check_progress(self):
        self.publisher.wait_for_upload_page().set_input_files(str(self.video))
        self.page.evaluate("""() => {
            document.getElementById('upload-status').textContent = '重新上传';
            const help = document.createElement('p');
            help.textContent = '点击发布后，如作品还在上传中，请勿关闭页面，等待上传发布完成。';
            const check = document.createElement('p');
            check.textContent = '检测中18%';
            document.body.append(help, check);
        }""")
        self.publisher.wait_upload_ready()
        self.assertTrue(self.publisher.report["checks"]["video_processed"])
        self.assertEqual(self.page.evaluate("state.published"), 0)

    def test_local_video_preview_alone_does_not_confirm_upload(self):
        self.publisher.wait_for_upload_page().set_input_files(str(self.video))
        self.page.evaluate("""() => {
            document.getElementById('upload-status').remove();
            const video = document.createElement('video');
            video.src = URL.createObjectURL(new Blob(['offline local preview'], {type: 'video/mp4'}));
            video.controls = true;
            video.width = 320;
            video.height = 180;
            document.body.append(video);
        }""")
        self.assertTrue(self.page.locator('video[src]').is_visible())
        self.publisher.config["upload_timeout_seconds"] = 1
        with self.assertRaises(BrowserStepError) as caught:
            self.publisher.wait_upload_ready()
        self.assertEqual(caught.exception.code, "video_processing_timeout")
        self.assertFalse(self.publisher.report["checks"].get("video_processed", False))
        self.assertEqual(self.page.evaluate("state.published"), 0)

    def test_actual_upload_or_processing_status_blocks_even_with_completion_marker(self):
        self.publisher.config["upload_timeout_seconds"] = 1
        for status in ("上传中", "视频上传中：63%", "正在上传视频 63%", "正在转码", "视频处理中", "文件解析中，请稍等..."):
            with self.subTest(status=status):
                self.publisher.wait_for_upload_page().set_input_files(str(self.video))
                self.page.evaluate("""status => {
                    const progress = document.createElement('p');
                    progress.textContent = status;
                    document.body.append(progress);
                }""", status)
                with self.assertRaises(BrowserStepError) as caught:
                    self.publisher.wait_upload_ready()
                self.assertEqual(caught.exception.code, "video_processing_timeout")
                self.assertFalse(self.publisher.report["checks"].get("video_processed", False))
                self.assertEqual(self.page.evaluate("state.published"), 0)

    def test_upload_ready_selector_override_is_preserved(self):
        self.publisher.wait_for_upload_page().set_input_files(str(self.video))
        self.page.locator('#upload-status').evaluate("el => { el.textContent = '自定义完成状态'; }")
        self.publisher.config["upload_ready"] = "#upload-status"
        self.publisher.wait_upload_ready()
        self.assertTrue(self.publisher.report["checks"]["video_processed"])
        self.assertEqual(self.page.evaluate("state.published"), 0)

    def test_declaration_only_uses_explicitly_configured_value(self):
        self.manifest["declaration"] = "内容由AI生成"
        report = self.publisher.run(publish=False)
        self.assertEqual(self.page.evaluate("state.declaration"), "内容由AI生成")
        self.assertTrue(report["checks"]["declaration"])

    def test_public_api_saves_failure_diagnostic_and_closes_context(self):
        self.manifest["collection"]["preferred"] = "不存在"
        created_contexts = []

        def offline_launch(playwright, profile_dir, channel):
            context = playwright.chromium.launch_persistent_context(str(profile_dir), channel="chrome", headless=True)
            context.route("**/*", lambda route: route.fulfill(
                body=FIXTURE.read_text(encoding="utf-8"), content_type="text/html",
            ))
            created_contexts.append(context)
            return context

        with patch("douyin_agent.browser._launch", side_effect=offline_launch), patch("playwright.sync_api.sync_playwright") as factory:
            factory.return_value.__enter__.return_value = self.playwright
            report = run_publish(self.manifest, self.root / "profile", publish=True,
                                 selector_config=dict(self.publisher.config), timeout_seconds=3)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error"]["code"], "required_collection_missing")
        self.assertTrue(Path(report["screenshot"]).is_file())
        self.assertTrue(Path(report["report_path"]).is_file())
        self.assertTrue(report["browser_closed"])
        self.assertEqual(created_contexts[0].pages, [])


if __name__ == "__main__":
    unittest.main()
