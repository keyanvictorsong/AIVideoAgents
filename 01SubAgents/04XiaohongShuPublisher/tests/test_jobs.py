import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from xhs_publisher.jobs import (
    ATTEMPT_FILENAME, DuplicateSubmission, begin_upload, event, fingerprint, load_job,
    metadata, preparation_guard, profile_lock, validate_cover, write_json,
)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("offline test"))
        self.network.start()
        self.addCleanup(self.network.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"local video fixture")
        self.cover = self.root / "cover.png"
        Image.new("RGB", (30, 40), "navy").save(self.cover)
        self.output = self.root / "job"
        self.manifest = self.output / "manifest.json"
        self.job = {
            "schema_version": 1, "platform": "xiaohongshu", "job_id": "fixture",
            "source": fingerprint(self.video), "video": str(self.video),
            "cover": str(self.cover), "cover_source": validate_cover(self.cover),
            "title": "已确认标题", "description": "正文", "tags": ["示例"],
            "output_dir": str(self.output), "state": "prepared", "history": [],
        }
        write_json(self.manifest, self.job)
        self.profile = self.root / "private" / "profile"

    def test_metadata_enforces_title_length_and_normalizes_tags(self):
        self.assertEqual(metadata(" 标题 ", " 正文 ", "#示例,示例，#训练")["tags"], ["示例", "训练"])
        self.assertEqual(metadata("文" * 20)["title"], "文" * 20)
        for title in ("文" * 21, "🏀" * 11, "一\n二", "一\r二", " "):
            with self.subTest(title=title), self.assertRaises(ValueError):
                metadata(title)
        for tags in (["有 空格"], ["话题!"], [42], {"话题": True}):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                metadata("标题", tags=tags)

    def test_load_checks_platform_and_video_identity(self):
        self.assertEqual(load_job(self.manifest)["source"], self.job["source"])
        self.job["platform"] = "douyin"
        write_json(self.manifest, self.job)
        with self.assertRaisesRegex(ValueError, "platform=xiaohongshu"):
            load_job(self.manifest)
        self.job["platform"] = "xiaohongshu"
        write_json(self.manifest, self.job)
        self.video.write_bytes(b"replaced video")
        with self.assertRaisesRegex(ValueError, "原视频已更换"):
            load_job(self.manifest)

    def test_wrong_aspect_and_replaced_cover_are_rejected(self):
        Image.new("RGB", (40, 30)).save(self.cover)
        with self.assertRaisesRegex(ValueError, "3:4"):
            load_job(self.manifest)
        Image.new("RGB", (30, 40), "red").save(self.cover)
        with self.assertRaisesRegex(ValueError, "封面已更换"):
            load_job(self.manifest)

    def test_failure_before_video_input_allows_retry(self):
        with self.assertRaises(TimeoutError):
            with preparation_guard(self.profile, self.job):
                raise TimeoutError("login not ready")
        self.assertFalse((self.output / ATTEMPT_FILENAME).exists())
        with preparation_guard(self.profile, self.job):
            pass

    def test_video_input_marker_blocks_retry_across_output_directories(self):
        with self.assertRaises(TimeoutError):
            with preparation_guard(self.profile, self.job):
                write_json(self.output / ATTEMPT_FILENAME, {"status": "uploading"})
                raise TimeoutError("upload outcome uncertain")
        ledger_path = self.profile.parent / f"{self.profile.name}-handoff-ledger.json"
        ledger = json.loads(ledger_path.read_text())
        self.assertEqual(ledger[self.job["source"]["sha256"]]["status"], "preparation_interrupted")
        self.job["output_dir"] = str(self.root / "another-job")
        with self.assertRaises(DuplicateSubmission):
            with preparation_guard(self.profile, self.job):
                self.fail("must not upload twice")

    def test_upload_start_is_durable_before_browser_continues(self):
        ledger_path = self.profile.parent / f"{self.profile.name}-handoff-ledger.json"
        with preparation_guard(self.profile, self.job):
            begin_upload(self.job, self.profile)
            ledger = json.loads(ledger_path.read_text())
            self.assertEqual(ledger[self.job["source"]["sha256"]]["status"], "upload_started")
            self.assertTrue((self.output / ATTEMPT_FILENAME).exists())
            with self.assertRaises(DuplicateSubmission):
                begin_upload(self.job, self.profile)

    def test_filled_is_a_pending_handoff_and_never_published(self):
        with preparation_guard(self.profile, self.job) as report:
            report["status"] = "filled"
        marker = json.loads((self.output / ATTEMPT_FILENAME).read_text())
        self.assertEqual(marker["status"], "awaiting_user_publish")
        with self.assertRaises(DuplicateSubmission):
            with preparation_guard(self.profile, self.job):
                self.fail("pending handoff must block")

    def test_same_profile_is_locked_during_login_or_upload(self):
        with profile_lock(self.profile):
            with self.assertRaisesRegex(ValueError, "另一个任务"):
                with preparation_guard(self.profile, self.job):
                    self.fail("profile must have one owner")

    def test_event_preserves_distinct_publication_state(self):
        event(self.job, "awaiting_user_publish", {"publication_verified": False})
        self.assertEqual(self.job["state"], "awaiting_user_publish")
        self.assertFalse(self.job["history"][-1]["details"]["publication_verified"])


if __name__ == "__main__":
    unittest.main()
