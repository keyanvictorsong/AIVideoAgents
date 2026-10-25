"""Session recovery with synthetic credentials and every browser URL intercepted."""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from xhs_publisher.session import (
    MAX_BACKUP_AGE_SECONDS, SESSION_FILENAME, SessionStateError,
    invalidate_saved_session, restore_saved_session, save_authenticated_session,
)


CREATOR = "https://creator.rednote.com"
CN_CREATOR = "https://creator.xiaohongshu.com"
HTML = """<!doctype html><script>
window.atApplicationStart = {
  local: Object.fromEntries(Object.entries(localStorage)),
  session: Object.fromEntries(Object.entries(sessionStorage))
};
</script><input type=file accept='video/mp4'>"""


class SessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            cls.playwright.stop()
            raise unittest.SkipTest(f"Chrome unavailable for offline session tests: {type(exc).__name__}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.profile = self.root / "dedicated-profile"
        self.profile.mkdir()
        self.contexts = []
        self.recoveries = []

    def tearDown(self):
        for recovery in self.recoveries:
            recovery.stop()
        for context in self.contexts:
            context.close()
        self.folder.cleanup()

    def context(self, *, persistent=False):
        if persistent:
            context = self.playwright.chromium.launch_persistent_context(
                str(self.profile), channel="chrome", headless=True, service_workers="block")
        else:
            context = self.browser.new_context(service_workers="block")
        context.route("**/*", lambda route: route.fulfill(body=HTML, content_type="text/html"))
        self.contexts.append(context)
        return context

    def page(self, context, origin=CREATOR):
        page = context.new_page()
        page.goto(origin + "/publish/publish")
        return page

    def seed(self, *, persistent=False):
        context = self.context(persistent=persistent)
        page = self.page(context)
        context.add_cookies([
            {"name": "session_fixture", "value": "synthetic-session-secret", "domain": ".rednote.com", "path": "/", "httpOnly": True, "secure": True, "sameSite": "Lax"},
            {"name": "persistent_fixture", "value": "synthetic-persistent-secret", "domain": ".rednote.com", "path": "/", "expires": time.time() + 3600, "httpOnly": True, "secure": True, "sameSite": "Lax"},
        ])
        page.evaluate("""() => {
            localStorage.setItem('local_auth', 'synthetic-local-secret');
            sessionStorage.setItem('session_auth', 'synthetic-session-storage-secret');
        }""")
        result = save_authenticated_session(context, self.profile, page)
        return context, page, result

    def restore(self, context, page):
        recovery = restore_saved_session(context, self.profile, page)
        self.recoveries.append(recovery)
        return recovery

    def snapshot(self):
        return json.loads((self.profile / SESSION_FILENAME).read_text(encoding="utf-8"))

    def write_snapshot(self, snapshot):
        (self.profile / SESSION_FILENAME).write_text(json.dumps(snapshot), encoding="utf-8")

    def test_verified_state_recovers_on_real_profile_close_and_restart(self):
        first, _, _ = self.seed(persistent=True)
        first.close()
        self.contexts.remove(first)
        second = self.context(persistent=True)
        page = second.new_page()
        recovery = self.restore(second, page)
        page.goto(CREATOR + "/publish/publish")
        recovery.stop()
        initial = page.evaluate("atApplicationStart")
        self.assertEqual(initial["local"]["local_auth"], "synthetic-local-secret")
        self.assertEqual(initial["session"]["session_auth"], "synthetic-session-storage-secret")
        cookies = {cookie["name"]: cookie for cookie in second.cookies()}
        self.assertEqual(cookies["session_fixture"]["value"], "synthetic-session-secret")
        self.assertEqual(cookies["session_fixture"]["expires"], -1)
        self.assertTrue(cookies["persistent_fixture"]["httpOnly"])

    def test_missing_values_recover_before_application_javascript(self):
        self.seed()
        second = self.context()
        page = second.new_page()
        recovery = self.restore(second, page)
        self.assertEqual(recovery.cookies_restored, 2)
        page.goto(CREATOR + "/publish/publish")
        initial = page.evaluate("atApplicationStart")
        self.assertEqual(initial["local"]["local_auth"], "synthetic-local-secret")
        self.assertEqual(initial["session"]["session_auth"], "synthetic-session-storage-secret")

    def test_newer_current_cookie_and_storage_are_authoritative(self):
        self.seed()
        second = self.context()
        second.add_cookies([{"name": "session_fixture", "value": "newer-browser-secret", "domain": ".rednote.com", "path": "/", "httpOnly": True, "secure": True}])
        page = self.page(second)
        page.evaluate("""() => {
            localStorage.setItem('local_auth', 'newer-local');
            sessionStorage.setItem('session_auth', 'newer-session');
        }""")
        recovery = self.restore(second, page)
        page.reload()
        self.assertEqual(recovery.cookies_restored, 1)
        initial = page.evaluate("atApplicationStart")
        self.assertEqual(initial["local"]["local_auth"], "newer-local")
        self.assertEqual(initial["session"]["session_auth"], "newer-session")
        self.assertEqual(next(cookie["value"] for cookie in second.cookies() if cookie["name"] == "session_fixture"), "newer-browser-secret")

    def test_expired_cookies_are_not_revived_or_given_later_expiry(self):
        self.seed()
        snapshot = self.snapshot()
        snapshot["cookies"][0]["expires"] = time.time() - 1
        original_expiry = snapshot["cookies"][1]["expires"]
        self.write_snapshot(snapshot)
        second = self.context()
        recovery = self.restore(second, second.new_page())
        cookies = second.cookies()
        self.assertEqual(recovery.cookies_restored, 1)
        self.assertEqual([cookie["name"] for cookie in cookies], ["persistent_fixture"])
        self.assertAlmostEqual(cookies[0]["expires"], original_expiry, places=4)

    def test_stale_backup_is_not_replayed(self):
        self.seed()
        snapshot = self.snapshot()
        snapshot["captured_at"] = time.time() - MAX_BACKUP_AGE_SECONDS - 1
        self.write_snapshot(snapshot)
        second = self.context()
        page = second.new_page()
        recovery = self.restore(second, page)
        self.assertEqual(recovery.status, "saved_session_too_old")
        page.goto(CREATOR + "/publish/publish")
        self.assertEqual(second.cookies(), [])
        self.assertEqual(page.evaluate("atApplicationStart"), {"local": {}, "session": {}})

    def test_snapshot_is_atomic_private_and_not_exported_with_safe_summary(self):
        original_replace = os.replace
        observed = []

        def replace(source, destination):
            observed.append((Path(source).parent, stat.S_IMODE(Path(source).stat().st_mode)))
            original_replace(source, destination)

        with patch("xhs_publisher.session.os.replace", side_effect=replace):
            _, _, summary = self.seed()
        self.assertEqual(observed, [(self.profile.resolve(), 0o600)])
        self.assertEqual(stat.S_IMODE((self.profile / SESSION_FILENAME).stat().st_mode), 0o600)
        self.assertEqual(list(self.root.iterdir()), [self.profile])
        self.assertEqual(list(self.profile.iterdir()), [self.profile / SESSION_FILENAME])
        self.assertNotIn("secret", json.dumps(summary))
        second = self.context()
        recovery = self.restore(second, second.new_page())
        self.assertNotIn("secret", repr(recovery))
        self.assertNotIn("secret", json.dumps(recovery.summary()))

    def test_failed_atomic_write_preserves_previous_snapshot_and_cleans_temp(self):
        context, page, _ = self.seed()
        path = self.profile / SESSION_FILENAME
        old = path.read_bytes()
        with patch("xhs_publisher.session.os.replace", side_effect=OSError("synthetic-secret-must-not-leak")):
            with self.assertRaises(SessionStateError) as caught:
                save_authenticated_session(context, self.profile, page)
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual(list(self.profile.iterdir()), [path])

    def test_storage_is_bound_to_exact_origin_and_unrelated_credentials_are_excluded(self):
        context, page, _ = self.seed()
        cn = self.page(context, CN_CREATOR)
        cn.evaluate("sessionStorage.setItem('cn_auth', 'synthetic-cn-secret')")
        unrelated = self.page(context, "https://unrelated.example")
        unrelated.evaluate("""() => {
            localStorage.setItem('unrelated', 'unrelated-secret');
            sessionStorage.setItem('unrelated', 'unrelated-secret');
        }""")
        context.add_cookies([{"name": "other", "value": "unrelated-secret", "domain": ".unrelated.example", "path": "/"}])
        save_authenticated_session(context, self.profile, page)
        self.assertNotIn("unrelated-secret", (self.profile / SESSION_FILENAME).read_text(encoding="utf-8"))
        second = self.context()
        target = second.new_page()
        recovery = self.restore(second, target)
        for origin in ("https://unrelated.example", "https://rednote.com.unrelated.example", "http://creator.rednote.com"):
            target.goto(origin)
            self.assertEqual(target.evaluate("atApplicationStart"), {"local": {}, "session": {}})
        target.goto(CN_CREATOR)
        initial = target.evaluate("atApplicationStart")
        self.assertEqual(initial["session"], {"cn_auth": "synthetic-cn-secret"})
        self.assertNotIn("session_auth", initial["session"])
        recovery.stop()

    def test_verified_tab_wins_over_other_tab_same_origin(self):
        context, page, _ = self.seed()
        other = self.page(context)
        other.evaluate("sessionStorage.setItem('session_auth', 'other-tab-secret')")
        save_authenticated_session(context, self.profile, page)
        entries = self.snapshot()["origins"][0]["sessionStorage"]
        self.assertEqual(entries, [{"name": "session_auth", "value": "synthetic-session-storage-secret"}])

    def test_stopped_initializer_cannot_resurrect_later_deleted_storage(self):
        self.seed()
        second = self.context()
        page = second.new_page()
        recovery = self.restore(second, page)
        page.goto(CREATOR)
        recovery.stop()
        page.evaluate("localStorage.clear(); sessionStorage.clear()")
        page.reload()
        self.assertEqual(page.evaluate("atApplicationStart"), {"local": {}, "session": {}})

    def test_confirmed_invalidation_removes_only_backup_without_clearing_browser(self):
        self.seed()
        second = self.context()
        page = second.new_page()
        recovery = self.restore(second, page)
        before = second.cookies()
        summary = invalidate_saved_session(self.profile, recovery)
        self.assertEqual(summary["status"], "saved_session_invalidated")
        self.assertFalse((self.profile / SESSION_FILENAME).exists())
        self.assertEqual(second.cookies(), before)
        page.goto(CREATOR)
        self.assertEqual(page.evaluate("atApplicationStart"), {"local": {}, "session": {}})

    def test_untrusted_verified_page_and_unsafe_snapshot_permissions_are_rejected(self):
        context = self.context()
        bad_page = self.page(context, "https://unrelated.example")
        with self.assertRaises(SessionStateError):
            save_authenticated_session(context, self.profile, bad_page)
        self.assertFalse((self.profile / SESSION_FILENAME).exists())
        self.seed()
        (self.profile / SESSION_FILENAME).chmod(0o644)
        with self.assertRaises(SessionStateError):
            self.restore(context, context.new_page())

    def test_corrupt_snapshot_error_does_not_quote_credential_content(self):
        path = self.profile / SESSION_FILENAME
        path.write_text('{"private": "synthetic-secret', encoding="utf-8")
        path.chmod(0o600)
        context = self.context()
        with self.assertRaises(SessionStateError) as caught:
            self.restore(context, context.new_page())
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_missing_snapshot_does_not_modify_browser(self):
        context = self.context()
        result = self.restore(context, context.new_page())
        self.assertEqual(result.status, "no_saved_session")
        self.assertEqual(context.cookies(), [])

    def test_cookie_protocol_error_does_not_expose_arguments(self):
        self.seed()
        context = self.context()
        with patch.object(context, "add_cookies", side_effect=RuntimeError("synthetic-session-secret")):
            with self.assertRaises(SessionStateError) as caught:
                self.restore(context, context.new_page())
        self.assertNotIn("synthetic-session-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
