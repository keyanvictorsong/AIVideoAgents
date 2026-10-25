"""Offline regression checks for login/captcha classification and XHS state."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dyxhs_guider import collectors


class PageAccessTests(unittest.TestCase):
    def classify(self, text, url="https://www.xiaohongshu.com/search_result?keyword=园艺", **kwargs):
        result = collectors.classify_page_access(text, url, **kwargs)
        return result[0] if result else None

    def test_xhs_login_dialog_sms_code_is_login_not_rate_limit(self):
        text = "小红书 - 你的生活指南\n登录后查看搜索结果\n手机号登录\n输入手机号\n输入验证码\n获取验证码\n登录"
        self.assertEqual(self.classify(text), "login_required")

    def test_phone_and_sms_fields_alone_identify_login_form(self):
        self.assertEqual(self.classify("小红书\n请输入手机号\n请输入验证码\n发送验证码"), "login_required")

    def test_verification_title_is_a_challenge_even_with_blank_body(self):
        for title in ("验证码中间页", "安全验证", "Captcha", "Security Check - 抖音", "小红书 - 安全验证"):
            self.assertEqual(self.classify(title + "\n"), "rate_limited", title)

    def test_strong_challenge_beats_visible_login_dialog(self):
        for challenge in ("安全验证", "请拖动滑块完成验证", "请求过于频繁，请稍后再试", "操作频繁", "访问过于频繁", "Verify you are human", "验证后继续", "为保证账号安全，请先完成安全验证"):
            self.assertEqual(self.classify("小红书\n手机号登录\n输入验证码\n" + challenge), "rate_limited", challenge)

    def test_caption_mentioning_verification_is_not_a_gate(self):
        for body in ("今天介绍验证码是什么，怎样在手机上接收验证码", "验证码\n这是视频正文中的普通词语", "程序员讲解 captcha 的实现原理"):
            self.assertIsNone(self.classify("小红书\n" + body), body)

    def test_sms_login_title_is_not_a_captcha_title(self):
        self.assertEqual(self.classify("手机号登录\n输入验证码\n获取验证码"), "login_required")
        self.assertIsNone(self.classify("验证码 - 实现原理教学\n今天介绍验证码的实现方法"))

    def test_query_keywords_do_not_create_challenge_or_login(self):
        self.assertIsNone(self.classify("抖音\n介绍验证码设计", "https://www.douyin.com/search/captcha设计?redirect=login"))
        self.assertEqual(self.classify("", "https://verifycenter.douyin.com/captcha"), "rate_limited")
        self.assertEqual(self.classify("", "https://www.xiaohongshu.com/login"), "login_required")

    def test_explicit_title_and_network_error(self):
        self.assertEqual(self.classify("", title="验证码中间页"), "rate_limited")
        self.assertEqual(self.classify("小红书\n网络连接异常"), "network_error")


class AdapterIsolationTests(unittest.TestCase):
    def setUp(self):
        self.source = collectors.legacy_module("sources")
        self.original_classifier = self.source._page_block
        self.original_state = self.source._XHS_STATE

    def test_own_adapter_is_used_without_mutating_legacy_module(self):
        def browser(result, queries, **kwargs):
            self.assertIs(kwargs["source_module"], self.source)
            result.status, result.reason = collectors.classify_page_access(
                '小红书\n登录后查看搜索结果\n输入验证码', 'https://www.xiaohongshu.com/search_result')
        with tempfile.TemporaryDirectory() as directory, patch.object(collectors, "_browser_collect", browser), patch.object(self.source, "_mcp_collect", return_value=False):
            result = collectors.collect_platform("xiaohongshu", output=Path(directory), queries=["园艺"])
        self.assertEqual(result["status"], "login_required")
        self.assertIs(self.source._page_block, self.original_classifier)
        self.assertEqual(self.source._XHS_STATE, self.original_state)

    def test_exception_cannot_leave_legacy_globals_changed(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(collectors, "_browser_collect", side_effect=RuntimeError('offline simulated failure')):
            with self.assertRaisesRegex(RuntimeError, "offline simulated failure"):
                collectors.collect_platform("douyin", output=Path(directory), queries=["园艺"])
        self.assertIs(self.source._page_block, self.original_classifier)
        self.assertEqual(self.source._XHS_STATE, self.original_state)


class HydrationTests(unittest.TestCase):
    class Page:
        url = 'https://www.douyin.com/search/园艺?type=video'

        def __init__(self, *, links_after=None, body='加载中', state_after=None):
            self.seconds = 0
            self.links_after = links_after
            self.body = body
            self.state_after = state_after

        def title(self):
            return '抖音搜索'

        def locator(self, selector):
            page = self
            class Locator:
                def inner_text(self, **kwargs):
                    return page.body

                def count(self):
                    return 16 if page.links_after is not None and page.seconds >= page.links_after else 0
            return Locator()

        def evaluate(self, script):
            return [{'id': 'one'}] if self.state_after is not None and self.seconds >= self.state_after else []

        def wait_for_timeout(self, milliseconds):
            self.seconds += milliseconds / 1000

    def wait(self, page, platform='douyin', **kwargs):
        return collectors._wait_for_hydration(page, platform, clock=lambda: page.seconds, **kwargs)

    def test_slow_hydration_waits_for_real_links_without_claiming_metrics(self):
        page = self.Page(links_after=7)
        result = self.wait(page)
        self.assertEqual(result['status'], 'links_present')
        self.assertEqual(result['result_link_count'], 16)
        self.assertGreaterEqual(result['waited_ms'], 7000)
        self.assertNotIn('valid_count', result)
        self.assertNotIn('metrics', result)

    def test_timeout_has_a_bound_and_is_not_a_no_results_claim(self):
        page = self.Page()
        result = self.wait(page, timeout_ms=15000)
        self.assertEqual(result['status'], 'hydration_timeout')
        self.assertEqual(page.seconds, 15)

    def test_login_and_http429_stop_without_waiting_out_budget(self):
        page = self.Page(body='登录后查看搜索结果\n手机号登录\n输入验证码')
        self.assertEqual(self.wait(page)['status'], 'login_required')
        self.assertEqual(page.seconds, 0)
        page = self.Page()
        self.assertEqual(self.wait(page, response_probe=lambda: {'rate_limited': True})['status'], 'rate_limited')
        self.assertEqual(page.seconds, 0)

    def test_response_and_hydrated_xhs_state_can_establish_readiness(self):
        page = self.Page()
        result = self.wait(page, response_probe=lambda: {'row_count': 3 if page.seconds >= 4 else 0})
        self.assertEqual(result['status'], 'response_received')
        page = self.Page(state_after=3)
        self.assertEqual(self.wait(page, 'xiaohongshu')['status'], 'state_received')

    def test_explicit_empty_message_is_distinct_from_timeout(self):
        self.assertEqual(self.wait(self.Page(body='暂无搜索结果'))['status'], 'no_results')


@unittest.skipUnless(shutil.which("node"), "Node is needed only for offline JavaScript regression checks")
class XhsStateTests(unittest.TestCase):
    def evaluate(self, state):
        script = "const window={__INITIAL_STATE__:JSON.parse(process.argv[1])}; const read=(" + collectors.XHS_STATE + "); process.stdout.write(JSON.stringify(read()));"
        result = subprocess.run([shutil.which("node"), "-e", script, json.dumps(state)], capture_output=True, text=True, timeout=5, check=True)
        return json.loads(result.stdout)

    def test_plain_array_and_both_vue_ref_shapes(self):
        rows = [{"id": "one", "noteCard": {"type": "video"}}]
        for feeds in (rows, {"value": rows}, {"_value": rows}):
            self.assertEqual(self.evaluate({"search": {"feeds": feeds}}), rows)

    def test_invalid_feeds_do_not_hide_available_detail_notes(self):
        note = {"id": "detail", "type": "video"}
        self.assertEqual(self.evaluate({"search": {"feeds": {"value": {}}}, "note": {"noteDetailMap": {"detail": {"note": note}}}}), [note])
        self.assertEqual(self.evaluate({"search": {"feeds": {"unexpected": []}}}), [])
        self.assertEqual(self.evaluate(None), [])


if __name__ == "__main__":
    unittest.main()
