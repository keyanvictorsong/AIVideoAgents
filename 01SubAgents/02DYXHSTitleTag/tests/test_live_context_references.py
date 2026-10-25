"""Offline checks for safe collection within an already-owned browser."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dyxhs_title_tag.live_context_references import (
    _observed_block, collect_xhs_in_context, record_shared_xhs_block,
)
from dyxhs_title_tag.sources import CollectionResult, ReferenceSample, _XHS_STATE


class FakePage:
    def __init__(self, text="", rows=()):
        self.text, self.rows = text, list(rows)
        self.url = "about:blank"
        self.closed = False
        self.navigations = []
        self.listeners = {}
        self.mouse = SimpleNamespace(wheel=lambda *_: None)

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        assert self.listeners.pop(event) == callback

    def goto(self, url, **_):
        self.url = url
        self.navigations.append(url)
        return SimpleNamespace(status=200)

    def title(self):
        return "只读搜索"

    def locator(self, selector):
        assert selector == "body"
        return SimpleNamespace(inner_text=lambda **_: self.text)

    def wait_for_timeout(self, _):
        pass

    def evaluate(self, script, *_):
        return self.rows if script == _XHS_STATE else []

    def screenshot(self, *, path, **_):
        Path(path).write_bytes(b"offline screenshot fixture")

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, search_page):
        self.existing_tab = FakePage("已有待发表单，必须保留")
        self.pages = [self.existing_tab]
        self.search_page = search_page
        self.closed = False

    def new_page(self):
        self.pages.append(self.search_page)
        return self.search_page

    def close(self):
        self.closed = True
        raise AssertionError("collector must not close the shared context")


class LiveContextReferenceTests(unittest.TestCase):
    def setUp(self):
        blocked_network = patch.object(socket.socket, "connect", side_effect=AssertionError("offline test"))
        blocked_network.start()
        self.addCleanup(blocked_network.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_sms_login_is_not_a_captcha(self):
        block = _observed_block("登录后查看搜索结果 手机号登录 获取验证码", "https://www.xiaohongshu.com/search_result")
        self.assertEqual(block[0], "login_required")
        self.assertIn("不要求用户", block[1])

    def test_real_security_verification_is_still_blocked(self):
        for text in ("请完成安全验证 获取验证码", "验证码中间页", "访问过于频繁"):
            with self.subTest(text=text):
                self.assertEqual(_observed_block(text, "https://www.xiaohongshu.com")[0], "rate_limited")

    def test_block_exit_preserves_context_existing_tab_and_collects_nothing(self):
        page = FakePage("登录后查看搜索结果 手机号登录 获取验证码")
        context = FakeContext(page)
        path = self.root / "first" / "xiaohongshu.json"
        result, returned_page = collect_xhs_in_context(context, ["桌面收纳", "整理工具"], path)
        saved = json.loads(path.read_text())
        self.assertIs(returned_page, page)
        self.assertEqual(result.status, "login_required")
        self.assertEqual(len(page.navigations), 1)
        self.assertEqual(saved["samples"], [])
        self.assertEqual(saved["days"], 7)
        self.assertEqual(saved["attempts"][0]["http_status"], 200)
        self.assertFalse(context.closed)
        self.assertFalse(context.existing_tab.closed)
        self.assertEqual(context.existing_tab.text, "已有待发表单，必须保留")
        self.assertFalse(page.closed)
        self.assertEqual(page.listeners, {})
        self.assertTrue((path.parent / "xiaohongshu_blocked.png").is_file())

    def test_shared_block_does_not_mix_samples_or_claim_second_request(self):
        old = CollectionResult(platform="xiaohongshu", status="login_required")
        old.samples.append(ReferenceSample(platform="xiaohongshu", id="old", url="https://www.xiaohongshu.com/explore/old"))
        old.attempts.append({"query": "桌面收纳", "http_status": 200, "status": "login_required"})
        path = self.root / "second" / "xiaohongshu.json"
        result = record_shared_xhs_block(old, self.root / "first.json", ["桌面 收纳"], path)
        data = json.loads(path.read_text())
        self.assertEqual(result.status, "login_required")
        self.assertEqual(data["samples"], [])
        self.assertEqual(data["valid_count"], 0)
        self.assertFalse(data["attempts"][0]["navigation_attempted"])
        self.assertEqual(data["attempts"][0]["adapter"], "existing_context_shared_block")
        self.assertEqual(data["attempts"][0]["source_attempt"]["query"], "桌面收纳")
        self.assertEqual(data["attempts"][0]["planned_queries"], ["桌面 收纳"])

    def test_30_day_expansion_explicitly_reevaluates_same_observed_samples(self):
        row = {"id": "datedvideo", "title": "桌面的收纳过程", "type": "video",
               "published_at": (datetime.now(timezone.utc) - timedelta(days=12)).isoformat()}
        page = FakePage("搜索结果", rows=[row])
        context = FakeContext(page)
        path = self.root / "dated" / "xiaohongshu.json"
        result, _ = collect_xhs_in_context(context, ["桌面 收纳"], path, max_pages=1, topic_keywords=["收纳"])
        data = json.loads(path.read_text())
        self.assertEqual(result.days, 30)
        self.assertEqual(result.valid_count, 1)
        self.assertEqual(len(page.navigations), 1)
        self.assertEqual(data["attempts"][0]["requested_window_days"], 7)
        self.assertEqual(data["window_evaluation"]["valid_count_7_days"], 0)
        self.assertTrue(data["window_evaluation"]["reevaluated_existing_samples"])
        self.assertFalse(data["window_evaluation"]["additional_requests_for_expansion"])
        self.assertEqual(data["samples"][0]["source_window_days"], 7)
        self.assertFalse(context.closed)


if __name__ == "__main__":
    unittest.main()
