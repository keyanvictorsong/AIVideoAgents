"""Offline regressions for reusable covers and neutral publishing defaults."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from douyin_agent.cli import parser, prepare
from douyin_agent.browser import _Publisher, BrowserStepError, run_publish
from douyin_agent.jobs import metadata


class PortablePublisherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.video = self.root / "example.mp4"
        self.video.write_bytes(b"synthetic video fixture, not uploaded")
        self.vertical = self.root / "vertical.png"
        self.horizontal = self.root / "horizontal.jpg"
        Image.new("RGB", (90, 120), "white").save(self.vertical)
        Image.new("RGB", (120, 90), "black").save(self.horizontal)
        self.output = self.root / "output"

    def arguments(self, *extra):
        return parser().parse_args(["prepare", str(self.video), "--title", "合成示例", "--output", str(self.output), *extra])

    def test_finished_covers_are_copied_unchanged_without_rendering(self):
        args = self.arguments("--cover-vertical", str(self.vertical), "--cover-horizontal", str(self.horizontal))
        with patch("douyin_agent.media.prepare_covers") as renderer, patch("builtins.print"):
            job = json.loads(prepare(args).read_text())
        renderer.assert_not_called()
        for kind, original in (("vertical", self.vertical), ("horizontal", self.horizontal)):
            actual = Path(job["covers"][kind])
            self.assertEqual(actual.parent, self.output)
            self.assertEqual(actual.read_bytes(), original.read_bytes())
            self.assertEqual(job["covers"]["source_fingerprints"][kind]["sha256"], hashlib.sha256(original.read_bytes()).hexdigest())
        self.assertEqual(job["hashtags"], [])
        self.assertEqual(job["description"], "")
        self.assertEqual(job["collection"], {"preferred": None, "keywords": [], "required": False})
        self.assertFalse((self.output / "publish-attempt.json").exists())

    def test_one_cover_rejects_before_creating_output(self):
        for option, path in (("--cover-vertical", self.vertical), ("--cover-horizontal", self.horizontal)):
            with self.subTest(option=option), self.assertRaisesRegex(ValueError, "成对"):
                prepare(self.arguments(option, str(path)))
            self.assertFalse(self.output.exists())

    def test_wrong_orientation_and_corrupt_images_reject_before_output(self):
        cases = [(self.horizontal, self.vertical)]
        corrupt = self.root / "corrupt.png"
        corrupt.write_bytes(b"not an image")
        cases.append((corrupt, self.horizontal))
        for vertical, horizontal in cases:
            with self.subTest(vertical=vertical), self.assertRaises(ValueError):
                prepare(self.arguments("--cover-vertical", str(vertical), "--cover-horizontal", str(horizontal)))
            self.assertFalse(self.output.exists())

    def test_missing_cover_rejects_before_output(self):
        with self.assertRaisesRegex(ValueError, "封面不存在"):
            prepare(self.arguments("--cover-vertical", str(self.root / "missing.png"), "--cover-horizontal", str(self.horizontal)))
        self.assertFalse(self.output.exists())

    def test_metadata_has_no_inherited_copy_or_tags(self):
        self.assertEqual(metadata("自定标题"), {"title": "自定标题", "description": "", "hashtags": []})
        self.assertEqual(parser().parse_args(["prepare", "video.mp4"]).title_mode, "preserve")

    def test_unrequested_collection_does_not_touch_page(self):
        publisher = _Publisher.__new__(_Publisher)
        publisher.report = {}
        publisher.manifest = {"collection": {"preferred": None, "keywords": [], "required": False}}
        publisher.choose_collection()  # No page supplied: selecting is forbidden.
        publisher.manifest["collection"]["required"] = True
        with self.assertRaises(BrowserStepError):
            publisher.choose_collection()

    def test_missing_selectors_rejects_before_starting_browser(self):
        with patch("playwright.sync_api.sync_playwright") as browser:
            with self.assertRaisesRegex(BrowserStepError, "首次使用"):
                run_publish({}, self.root / "profile", selector_config={})
        browser.assert_not_called()
        self.assertFalse((self.root / "profile").exists())


if __name__ == "__main__":
    unittest.main()
