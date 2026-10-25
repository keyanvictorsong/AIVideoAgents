"""Offline Apple Events protocol and browser-side public-field projection tests."""
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from dyxhs_guider.chrome_reader import ChromeReadError, ChromeTab, PUBLIC_STATE


class ChromeTabTests(unittest.TestCase):
    def test_execute_uses_stdin_without_shell_and_fixed_ids(self):
        result = subprocess.CompletedProcess(["osascript"], 0, '{"value":{"count":2}}', '')
        with patch("dyxhs_guider.chrome_reader.subprocess.run", return_value=result) as run:
            value = ChromeTab(123, 456).evaluate('() => ({count:2, text:"quoted\\ntext"})')
        self.assertEqual(value, {"count": 2})
        args, kwargs = run.call_args
        self.assertEqual(args, (["osascript"],))
        self.assertNotIn("shell", kwargs)
        script = kwargs["input"]
        self.assertIn('tab id "123" of window id "456"', script)
        self.assertIn('set researchURL to URL of researchTab', script)
        self.assertIn('return execute researchTab javascript', script)
        for forbidden in ("repeat with", "every tab", "URL of tabs", "do shell script"):
            self.assertNotIn(forbidden, script)

    def test_no_window_id_uses_front_window_only(self):
        result = subprocess.CompletedProcess(["osascript"], 0, '{"value":null}', '')
        with patch("dyxhs_guider.chrome_reader.subprocess.run", return_value=result) as run:
            self.assertIsNone(ChromeTab(12).evaluate('window.scrollBy(0, 100)'))
        self.assertIn('tab id "12" of front window', run.call_args.kwargs["input"])

    def test_disabled_javascript_error_12_is_explicit_and_raw_error_is_hidden(self):
        result = subprocess.CompletedProcess(["osascript"], 1, '', 'Chrome error signed?xsec_token=secret (12)')
        with patch("dyxhs_guider.chrome_reader.subprocess.run", return_value=result):
            state = ChromeTab(12).read_public_state()
        self.assertEqual(state["status"], "javascript_disabled")
        self.assertNotIn("secret", json.dumps(state))
        self.assertEqual(state["poll_count"], 1)

    def test_missing_tab_does_not_fall_back_to_another_tab(self):
        result = subprocess.CompletedProcess(["osascript"], 1, '', "Can't get tab id 12. (-1728)")
        with patch("dyxhs_guider.chrome_reader.subprocess.run", return_value=result) as run:
            state = ChromeTab(12, 34).read_public_state()
        self.assertEqual(state["status"], "tab_not_found")
        self.assertEqual(run.call_count, 1)

    def test_create_is_xhs_only_and_returns_fixed_ids(self):
        with patch("dyxhs_guider.chrome_reader.subprocess.run") as run:
            for url in ("https://example.com", "https://www.xiaohongshu.com.evil.test/explore/id", "javascript:alert(1)"):
                with self.assertRaises(ValueError):
                    ChromeTab.create(url)
            run.assert_not_called()
            run.return_value = subprocess.CompletedProcess(["osascript"], 0, '456:123', '')
            tab = ChromeTab.create("https://www.xiaohongshu.com/search_result?keyword=园艺")
        self.assertEqual((tab.tab_id, tab.window_id), (123, 456))
        script = run.call_args.kwargs["input"]
        self.assertIn("make new tab", script)
        self.assertNotIn("repeat with", script)

    def test_navigation_keeps_signed_input_private_and_checks_current_tab_origin(self):
        result = subprocess.CompletedProcess(["osascript"], 0, 'navigated', '')
        with patch("dyxhs_guider.chrome_reader.subprocess.run", return_value=result) as run:
            state = ChromeTab(12, 34).navigate("https://www.xiaohongshu.com/explore/abc?xsec_token=secret#foo")
        self.assertEqual(state, {"status": "navigated", "url": "https://www.xiaohongshu.com/explore/abc"})
        script = run.call_args.kwargs["input"]
        self.assertLess(script.index('DYXHS_UNEXPECTED_ORIGIN'), script.index('set URL of researchTab'))

    def test_gate_stops_before_exporting_rows_or_page_body(self):
        probe = {"_page": {"title": "小红书", "text": "登录后查看搜索结果\n手机号登录\n请输入验证码", "url": "https://www.xiaohongshu.com/search_result"},
                 "rows": [{"id": "not_exported"}], "links": []}
        with patch.object(ChromeTab, "evaluate", return_value=probe) as evaluate:
            state = ChromeTab(12).read_public_state()
        self.assertEqual(state["status"], "login_required")
        self.assertEqual(state["rows"], [])
        self.assertNotIn("_page", state)
        self.assertEqual(evaluate.call_count, 1)

    def test_hydration_wait_has_a_bound_and_does_not_claim_no_results(self):
        seconds = [0.0]
        def sleep(duration):
            seconds[0] += duration
        probe = {"_page": {"title": "小红书", "text": "加载中", "url": "https://www.xiaohongshu.com/search_result"}, "rows": [], "links": []}
        with patch("dyxhs_guider.chrome_reader.time.monotonic", side_effect=lambda: seconds[0]), patch("dyxhs_guider.chrome_reader.time.sleep", side_effect=sleep), patch.object(ChromeTab, "evaluate", return_value=probe):
            state = ChromeTab(12).read_public_state(timeout_ms=1200, poll_ms=500)
        self.assertEqual(state["status"], "hydration_timeout")
        self.assertEqual(state["waited_ms"], 1200)
        self.assertEqual(seconds[0], 1.2)

    def test_real_state_is_ready_without_guessed_fields(self):
        row = {"id": "one", "url": None, "published_at_raw": None}
        probe = {"_page": {"title": "小红书", "text": "", "url": "https://www.xiaohongshu.com/search_result"}, "rows": [row], "links": []}
        with patch.object(ChromeTab, "evaluate", return_value=probe):
            state = ChromeTab(12).read_public_state()
        self.assertEqual(state["status"], "state_received")
        self.assertEqual(state["rows"], [row])
        self.assertNotIn("_page", state)


@unittest.skipUnless(shutil.which("node"), "Node is used only for offline JavaScript checks")
class PublicProjectionTests(unittest.TestCase):
    def project(self, state, links=(), url="https://www.xiaohongshu.com/search_result?keyword=园艺"):
        fixture = {"state": state, "links": links, "url": url}
        script = """const f=JSON.parse(process.argv[1]);
const location=new URL(f.url);const window={__INITIAL_STATE__:f.state};
const document={title:'小红书',body:{innerText:'公开页面'},querySelectorAll:()=>f.links.map(x=>({getAttribute:k=>x[k]??null}))};
process.stdout.write(JSON.stringify((""" + PUBLIC_STATE + ")()));"
        result = subprocess.run([shutil.which("node"), "-e", script, json.dumps(fixture)], capture_output=True, text=True, timeout=5, check=True)
        return json.loads(result.stdout)

    def test_state_whitelist_drops_tokens_media_and_credential_fields(self):
        state = {"search": {"feeds": {"_value": [{"id": "abc", "xsecToken": "outer-secret", "noteCard": {
            "displayTitle": "园艺成长", "type": "video", "desc": "看 https://media.example/video?signature=media-secret", "time": 123456,
            "user": {"userId": "author1", "nickname": "作者", "access_token": "credential-secret"},
            "interactInfo": {"likedCount": "1.2万", "commentCount": 0},
            "video": {"url": "https://media.example/private?signature=video-secret"}}}]}}}
        result = self.project(state, [{"href": "/explore/abc?xsec_token=link-secret", "title": "园艺成长"}])
        row = result["rows"][0]
        self.assertEqual(row["url"], "https://www.xiaohongshu.com/explore/abc")
        self.assertEqual(row["metrics"]["likes"], "1.2万")
        self.assertEqual(row["metrics"]["comments"], 0)
        self.assertIsNone(row["metrics"]["views"])
        self.assertEqual(row["author_name"], "作者")
        self.assertEqual(row["description"], "看 https://media.example/video")
        self.assertEqual(set(row), {"id", "url", "title", "description", "author_id", "author_name", "content_type", "published_at_raw", "metrics"})
        exported = json.dumps(result)
        for forbidden in ("secret", "xsecToken", "xsec_token", "access_token", "signature", "credential"):
            self.assertNotIn(forbidden, exported)

    def test_state_id_does_not_create_a_link_and_missing_values_stay_null(self):
        result = self.project({"search": {"feeds": [{"id": "unlinked", "noteCard": {"displayTitle": "运动"}}]}})
        self.assertEqual(result["links"], [])
        row = result["rows"][0]
        self.assertIsNone(row["url"])
        self.assertIsNone(row["published_at_raw"])
        self.assertIsNone(row["content_type"])
        self.assertTrue(all(value is None for value in row["metrics"].values()))

    def test_current_detail_url_is_observed_evidence_and_external_links_are_excluded(self):
        result = self.project({"note": {"noteDetailMap": {"abc": {"note": {"id": "abc", "type": "video"}}}}},
                              [{"href": "https://evil.test/explore/other"}],
                              "https://www.xiaohongshu.com/explore/abc?xsec_token=secret")
        self.assertEqual(result["links"], [{"id": "abc", "url": "https://www.xiaohongshu.com/explore/abc", "title": None}])
        self.assertEqual(result["rows"][0]["url"], result["links"][0]["url"])


if __name__ == "__main__":
    unittest.main()
