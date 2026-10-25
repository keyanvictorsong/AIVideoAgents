import contextlib
import io
import json
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from xhs_publisher import cli
from xhs_publisher.jobs import ATTEMPT_FILENAME, write_json


class Terminal(io.StringIO):
    def isatty(self):
        return True


class CliTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("offline test"))
        network.start()
        self.addCleanup(network.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        root_patch = patch.object(cli, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        default_patch = patch.object(cli, "DEFAULT_PROFILE", self.root / ".local" / "browser-profile")
        default_patch.start()
        self.addCleanup(default_patch.stop)
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"local video fixture")
        self.cover = self.root / "cover.png"
        Image.new("RGB", (30, 40), "navy").save(self.cover)
        self.output = self.root / "job"
        self.manifest = self.output / "manifest.json"
        self.profile = self.root / "private-profile"
        self.browser = types.ModuleType("xhs_publisher.browser")
        self.browser.run_prepare = Mock(return_value={"status": "awaiting_user_publish"})
        self.browser.login = Mock(return_value={"status": "authenticated"})
        browser_patch = patch.dict(sys.modules, {"xhs_publisher.browser": self.browser})
        browser_patch.start()
        self.addCleanup(browser_patch.stop)

    def run_cli(self, arguments, *, tty=True):
        output = Terminal() if tty else io.StringIO()
        errors = io.StringIO()
        input_stream = Terminal() if tty else io.StringIO()
        with patch.object(sys, "stdin", input_stream), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = cli.main(arguments)
        return status, output.getvalue(), errors.getvalue()

    def prepare(self, extra=()):
        result = self.run_cli([
            "--profile", str(self.profile), "prepare", str(self.video),
            "--cover", str(self.cover), "--title", "已确认标题", "--description", "已确认正文",
            "--tags", "#示例,训练", "--output", str(self.output), *extra,
        ])
        self.assertEqual(result[0], 0, result)
        return json.loads(self.manifest.read_text())

    def test_prepare_writes_reusable_manifest_without_launching_browser(self):
        job = self.prepare()
        self.assertEqual(job["platform"], "xiaohongshu")
        self.assertEqual(job["state"], "prepared")
        self.assertEqual(job["tags"], ["示例", "训练"])
        self.assertEqual(job["browser_profile"], str(self.profile))
        self.assertEqual(len(job["source"]["sha256"]), 64)
        self.browser.run_prepare.assert_not_called()
        self.browser.login.assert_not_called()

    def test_upload_without_tty_does_not_start_browser_or_write_attempt(self):
        self.prepare()
        result = self.run_cli(["upload", str(self.manifest)], tty=False)
        self.assertEqual(result[0], 1)
        self.assertIn("TTY", result[2])
        self.browser.run_prepare.assert_not_called()
        self.assertFalse((self.output / ATTEMPT_FILENAME).exists())
        self.assertEqual(json.loads(self.manifest.read_text())["state"], "prepared")

    def test_no_publish_or_no_review_options_exist(self):
        for option in ("--publish", "--no-review"):
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(["upload", "manifest.json", option])
                self.assertEqual(caught.exception.code, 2)
        self.browser.run_prepare.assert_not_called()

    def test_filled_result_writes_pending_state_and_does_not_export_credentials(self):
        self.prepare()
        self.browser.run_prepare.return_value = {"status": "filled", "cookies": "never-export-this"}
        result = self.run_cli(["upload", str(self.manifest)])
        self.assertEqual(result[0], 0, result)
        job = json.loads(self.manifest.read_text())
        self.assertEqual(job["state"], "awaiting_user_publish")
        self.assertFalse(job["history"][-1]["details"]["publication_verified"])
        self.assertNotIn("never-export-this", self.manifest.read_text() + result[1] + result[2])
        args, keywords = self.browser.run_prepare.call_args
        self.assertEqual(args[1], self.profile)
        self.assertEqual(keywords["timeout_seconds"], 600)
        self.assertNotIn("publish", keywords)
        self.assertEqual(self.run_cli(["upload", str(self.manifest)])[0], 1)
        self.assertEqual(self.browser.run_prepare.call_count, 1)

    def test_manifest_profile_is_overridden_only_when_explicit(self):
        write_json(self.root / ".local" / "settings.json", {"browser_profile": str(self.root / "saved-profile")})
        self.prepare()
        explicit = self.root / "existing-personal-xhs-profile"
        status, _, errors = self.run_cli(["--profile", str(explicit), "upload", str(self.manifest)])
        self.assertEqual(status, 0, errors)
        self.assertEqual(self.browser.run_prepare.call_args.args[1], explicit)

    def test_default_profile_uses_project_fallback_when_settings_are_absent(self):
        self.assertEqual(cli.default_profile(), cli.DEFAULT_PROFILE)

    def test_saved_profile_is_used_by_prepare_and_login(self):
        saved = self.root / "existing-xhs-profile"
        write_json(self.root / ".local" / "settings.json", {"browser_profile": str(saved)})
        result = self.run_cli([
            "prepare", str(self.video), "--cover", str(self.cover), "--title", "已确认标题",
            "--description", "正文", "--output", str(self.output),
        ])
        self.assertEqual(result[0], 0, result)
        self.assertEqual(json.loads(self.manifest.read_text())["browser_profile"], str(saved))
        result = self.run_cli(["login"])
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.browser.login.call_args.args[0], saved)

    def test_login_uses_saved_creator_url(self):
        creator_url = "https://creator.rednote.com/publish/publish"
        write_json(self.root / ".local" / "settings.json", {
            "browser_profile": str(self.profile), "creator_url": creator_url,
        })
        result = self.run_cli(["login", "--timeout", "120"])
        self.assertEqual(result[0], 0, result)
        self.browser.login.assert_called_once_with(
            self.profile, channel="chrome", timeout_seconds=120, upload_url=creator_url,
        )

    def test_login_without_saved_creator_url_keeps_browser_default(self):
        result = self.run_cli(["login"])
        self.assertEqual(result[0], 0, result)
        self.assertNotIn("upload_url", self.browser.login.call_args.kwargs)

    def test_manifest_profile_wins_over_saved_settings(self):
        self.prepare()
        write_json(self.root / ".local" / "settings.json", {"browser_profile": str(self.root / "saved-profile")})
        result = self.run_cli(["upload", str(self.manifest)])
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.browser.run_prepare.call_args.args[1], self.profile)

    def test_upload_without_manifest_profile_uses_saved_settings(self):
        job = self.prepare()
        del job["browser_profile"]
        write_json(self.manifest, job)
        saved = self.root / "existing-xhs-profile"
        write_json(self.root / ".local" / "settings.json", {"browser_profile": str(saved)})
        result = self.run_cli(["upload", str(self.manifest)])
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.browser.run_prepare.call_args.args[1], saved)

    def test_upload_without_saved_or_manifest_profile_uses_project_fallback(self):
        job = self.prepare()
        del job["browser_profile"]
        write_json(self.manifest, job)
        result = self.run_cli(["upload", str(self.manifest)])
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.browser.run_prepare.call_args.args[1], cli.DEFAULT_PROFILE)

    def test_selectors_and_timeout_are_passed_without_publish_flag(self):
        self.prepare()
        selectors = self.root / "selectors.json"
        write_json(selectors, {"video_input": "input[type=file]"})
        result = self.run_cli(["--selectors", str(selectors), "upload", str(self.manifest), "--timeout", "91"])
        self.assertEqual(result[0], 0, result)
        kwargs = self.browser.run_prepare.call_args.kwargs
        self.assertEqual(kwargs["selector_config"], {"video_input": "input[type=file]"})
        self.assertEqual(kwargs["timeout_seconds"], 91)

    def test_upload_uses_saved_selectors_by_default(self):
        self.prepare()
        saved = {
            "upload_url": "https://creator.rednote.com/publish/publish",
            "video_input": "input[data-kind=video]",
        }
        write_json(self.root / ".local" / "selectors.json", saved)
        result = self.run_cli(["upload", str(self.manifest)])
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.browser.run_prepare.call_args.kwargs["selector_config"], saved)

    def test_explicit_selectors_override_matching_saved_keys_and_keep_others(self):
        self.prepare()
        write_json(self.root / ".local" / "selectors.json", {
            "upload_url": "https://creator.rednote.com/publish/publish",
            "video_input": "input[data-kind=old]",
        })
        override = self.root / "custom-selectors.json"
        write_json(override, {"video_input": "input[data-kind=new]", "title_input": "input.title"})
        result = self.run_cli(["--selectors", str(override), "upload", str(self.manifest)])
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.browser.run_prepare.call_args.kwargs["selector_config"], {
            "upload_url": "https://creator.rednote.com/publish/publish",
            "video_input": "input[data-kind=new]", "title_input": "input.title",
        })

    def test_pre_upload_failure_remains_retryable(self):
        self.prepare()
        self.browser.run_prepare.side_effect = TimeoutError("login not ready")
        self.assertEqual(self.run_cli(["upload", str(self.manifest)])[0], 1)
        self.assertEqual(json.loads(self.manifest.read_text())["state"], "preparation_failed")
        self.browser.run_prepare.side_effect = None
        self.assertEqual(self.run_cli(["upload", str(self.manifest)])[0], 0)

    def test_failure_after_video_input_blocks_retry(self):
        self.prepare()

        def interrupted(job, _profile, **_kwargs):
            write_json(Path(job["output_dir"]) / ATTEMPT_FILENAME, {"status": "uploading"})
            raise TimeoutError("upload started")

        self.browser.run_prepare.side_effect = interrupted
        self.assertEqual(self.run_cli(["upload", str(self.manifest)])[0], 1)
        self.assertEqual(json.loads(self.manifest.read_text())["state"], "preparation_interrupted")
        self.assertEqual(self.run_cli(["upload", str(self.manifest)])[0], 1)
        self.assertEqual(self.browser.run_prepare.call_count, 1)

    def test_report_cannot_claim_published_without_read_only_verification(self):
        self.prepare()
        self.browser.run_prepare.return_value = {"status": "published"}
        self.assertEqual(self.run_cli(["upload", str(self.manifest)])[0], 1)
        job = json.loads(self.manifest.read_text())
        self.assertEqual(job["state"], "manual_publication_unverified")
        self.assertFalse(job["history"][-1]["details"]["publication_verified"])

    def test_prepare_does_not_overwrite_pending_handoff(self):
        self.prepare()
        self.assertEqual(self.run_cli(["upload", str(self.manifest)])[0], 0)
        before = self.manifest.read_text()
        result = self.run_cli([
            "prepare", str(self.video), "--cover", str(self.cover), "--title", "另一个标题",
            "--description", "正文", "--output", str(self.output),
        ])
        self.assertEqual(result[0], 1)
        self.assertEqual(self.manifest.read_text(), before)


if __name__ == "__main__":
    unittest.main()
