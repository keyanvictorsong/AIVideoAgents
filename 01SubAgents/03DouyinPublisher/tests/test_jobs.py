import tempfile
import unittest
from pathlib import Path

from douyin_agent.jobs import DuplicateSubmission, fingerprint, load_job, metadata, submission_guard, write_json


class PublishingJobTests(unittest.TestCase):
    def test_caption_limit_includes_hashtags_and_emoji(self):
        with self.assertRaises(ValueError):
            metadata("示例", "球" * 998, ["示例"])
        with self.assertRaises(ValueError):
            metadata("🏀" * 16)

    def test_modified_video_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "video.mp4"
            video.write_bytes(b"old")
            job = {"schema_version": 1, "video": str(video), "source": fingerprint(video),
                   "title": "示例", "description": "", "hashtags": [], "covers": {}, "collection": {}}
            write_json(root / "manifest.json", job)
            video.write_bytes(b"new")
            with self.assertRaisesRegex(ValueError, "原视频已更换"):
                load_job(root / "manifest.json")

    def test_ambiguous_publish_prevents_retry_across_output_folders(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "job1"
            output.mkdir()
            job = {"source": {"sha256": "abc"}, "job_id": "abc", "title": "示例",
                   "output_dir": str(output), "state": "prepared"}
            profile = root / "browser"
            with self.assertRaises(TimeoutError):
                with submission_guard(profile, job):
                    (output / "publish-attempt.json").write_text("{}")
                    raise TimeoutError("click succeeded; response lost")
            job["output_dir"] = str(root / "job2")
            with self.assertRaises(DuplicateSubmission):
                with submission_guard(profile, job):
                    self.fail("must not retry")

    def test_failure_before_submit_allows_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            job = {"source": {"sha256": "abc"}, "job_id": "abc", "title": "示例",
                   "output_dir": str(root / "job"), "state": "prepared"}
            profile = root / "browser"
            with submission_guard(profile, job) as report:
                report.update(status="failed")
            with submission_guard(profile, job):
                pass


if __name__ == "__main__":
    unittest.main()
