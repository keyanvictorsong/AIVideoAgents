"""Single-video delivery state, source provenance and missing-cover recovery."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from talking_editor import cli, delivery
from talking_editor.pipeline import _digest_file, _write_json


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "一个 视频.MOV"
        self.source.write_bytes(b"source footage")
        self.output = self.root / "一个 视频_output"
        self.output.mkdir()
        for name in ("edited.mp4", "clean.mp4", "cover.png", "cover-reference.jpg", "captions.ass", "captions.srt"):
            (self.output / name).write_bytes(name.encode())
        self.report = {"status": "complete", "source": str(self.source), "source_sha256": _digest_file(self.source),
                       "removed_seconds": 0, "fillers_audio_removed": 0,
                       "artifacts": ["edited.mp4", "clean.mp4", "captions.ass", "captions.srt"]}
        _write_json(self.output / "report.json", self.report)
        _write_json(self.output / "settings.json", {"captions": {"title": "这是虚构的测试标题"}})
        _write_json(self.output / "cover-meta.json", {"title": "这是虚构的测试标题", "source_frame_seconds": 0.5})
        _write_json(self.output / "captions.json", [{"start": 0.1, "end": 1.8, "text": "有用的内容"}])
        _write_json(self.output / "transcript.json", {"segments": [{"start": 0, "end": 2, "text": "虚构口播测试内容"}]})
        _write_json(self.output / "edit-plan.json", {"keep": []})
        self.info = {"duration": 2., "video_duration": 2., "audio_duration": 2., "width": 1080,
                     "height": 1920, "fps": 30., "hdr": False, "rotation": 0, "has_audio": True}
        self.images = {}
        for target, name, kwargs in (
            (delivery.media, "probe", {"side_effect": self.probe}),
            (delivery.media, "_run", {"return_value": ""}),
            (delivery.media, "_binary", {"return_value": "ffmpeg"}),
            (delivery, "extract_cover_frame", {}),
            (delivery, "edit_video", {"return_value": {**self.report, "skipped": True}}),
        ):
            patcher = patch.object(target, name, **kwargs)
            setattr(self, name + "_mock", patcher.start())
            self.addCleanup(patcher.stop)

    def probe(self, path):
        if Path(path).resolve() in self.images:
            return self.images[Path(path).resolve()]
        return {**self.info, "video_codec": "png" if Path(path).suffix == ".png" else "h264"}

    def publication_pair(self):
        paths = {}
        for role, (width, height, _) in delivery.PUBLICATION_COVERS.items():
            codec, suffix = ("png", ".png") if role == "vertical" else ("mjpeg", ".jpg")
            path = self.root / (role + suffix)
            path.write_bytes((role + " approved artwork").encode())
            self.images[path] = {**self.info, "width": width, "height": height,
                                 "has_audio": False, "video_codec": codec}
            # finish() may subsequently revalidate the copied publication files.
            self.images[self.output / ("cover-" + role + suffix)] = self.images[path]
            paths[role] = path
        return {"cover_vertical_source": paths["vertical"], "cover_horizontal_source": paths["horizontal"]}

    def test_prepare_defaults_to_source_neighbor_and_preserves_approved_assets(self):
        originals = {name: (self.output / name).read_bytes() for name in ("edited.mp4", "cover.png", "captions.ass", "report.json")}
        job = delivery.prepare(self.source)
        self.assertEqual(Path(job["output_dir"]), self.output)
        self.assertEqual(job["cover_time"], 0.5)
        self.assertEqual(job["status"], "rendered")
        self.edit_video_mock.assert_not_called()
        self.extract_cover_frame_mock.assert_not_called()
        for name, content in originals.items():
            self.assertEqual((self.output / name).read_bytes(), content)

    def test_same_filename_with_changed_content_does_not_reuse_output(self):
        self.source.write_bytes(b"different footage")
        with self.assertRaisesRegex(ValueError, "different source"):
            delivery.prepare(self.source)
        self.edit_video_mock.assert_not_called()

    def test_cover_color_setting_refreshes_reference_at_its_selected_time_once(self):
        originals = {name: (self.output / name).read_bytes()
                     for name in ("edited.mp4", "captions.ass", "captions.srt", "transcript.json")}
        _write_json(self.output / "settings.json", {"captions": {"title": "这是虚构的测试标题"},
                                                   "render": {"tonemap_desat": 0}})
        job = delivery.prepare(self.source)
        self.extract_cover_frame_mock.assert_called_once_with(
            self.source, self.output / "cover-reference.jpg", .5, options={"tonemap_desat": 0})
        self.assertEqual(job["cover_color_options"], {"tonemap_desat": 0})
        self.extract_cover_frame_mock.reset_mock()
        delivery.prepare(self.source)
        self.extract_cover_frame_mock.assert_not_called()
        self.edit_video_mock.assert_not_called()
        for name, content in originals.items():
            self.assertEqual((self.output / name).read_bytes(), content)

    def test_interrupted_render_resumes_cached_analysis_without_analyze_rewrites(self):
        _write_json(self.output / "report.json", {**self.report, "status": "rendering"})
        original_report = (self.output / "report.json").read_bytes()
        delivery.prepare(self.source)
        self.edit_video_mock.assert_not_called()
        self.assertEqual((self.output / "report.json").read_bytes(), original_report)
        delivery.render(self.source, "这是虚构的测试标题")
        self.assertTrue(self.edit_video_mock.call_args.kwargs["overwrite"])

    def test_empty_transcript_cannot_be_used_to_invent_a_title(self):
        _write_json(self.output / "transcript.json", {"segments": [], "words": []})
        with self.assertRaisesRegex(ValueError, "No usable speech"):
            delivery.prepare(self.source)

    def test_finish_requires_cover_even_when_video_report_is_complete(self):
        (self.output / "cover.png").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            delivery.finish(self.source)
        self.assertFalse((self.output / "delivery.json").exists())

    def test_mismatched_title_rejected_before_replacing_cover(self):
        image = self.root / "new.png"
        image.write_bytes(b"new image")
        old = (self.output / "cover.png").read_bytes()
        with self.assertRaisesRegex(ValueError, "title must match"):
            delivery.finish(self.source, cover_source=image, cover_title="另一个标题", overwrite=True)
        self.assertEqual((self.output / "cover.png").read_bytes(), old)

    def test_finish_rejects_missing_render_artifacts(self):
        (self.output / "clean.mp4").unlink()
        with self.assertRaisesRegex(ValueError, "missing an artifact"):
            delivery.finish(self.source)

    def test_finished_job_tracks_video_and_cover_and_detects_removed_cover(self):
        job = delivery.finish(self.source)
        self.assertEqual(job["status"], "complete")
        self.assertIn("cover.png", job["artifacts"])
        self.assertEqual(job["cover_sha256"], _digest_file(self.output / "cover.png"))
        self.assertEqual(delivery.prepare(self.source)["status"], "complete")
        (self.output / "cover.png").unlink()
        self.assertEqual(delivery.prepare(self.source)["status"], "rendered")

    def test_changed_cover_bytes_invalidate_completed_delivery_state(self):
        delivery.finish(self.source)
        (self.output / "cover.png").write_bytes(b"another cover")
        self.assertEqual(delivery.prepare(self.source)["status"], "rendered")

    def test_publication_pair_accepts_exact_png_and_jpeg_and_tracks_both_without_legacy_copy(self):
        (self.output / "cover.png").unlink()
        pair = self.publication_pair()
        job = delivery.finish(self.source, **pair)
        self.assertEqual(job["status"], "complete")
        self.assertNotIn("cover_sha256", job)
        self.assertEqual(set(job["covers"]), {"vertical", "horizontal"})
        self.assertFalse((self.output / "cover.png").exists())
        for role, (width, height, ratio) in delivery.PUBLICATION_COVERS.items():
            item = job["covers"][role]
            self.assertEqual((item["width"], item["height"], item["ratio"]), (width, height, ratio))
            self.assertEqual(item["sha256"], _digest_file(self.output / item["image"]))
            self.assertIn(item["image"], job["artifacts"])
        verification = json.loads((self.output / "verification.json").read_text())
        self.assertEqual(verification["cover"]["covers"], job["covers"])
        self.assertEqual(delivery.prepare(self.source)["status"], "complete")
        self.assertEqual(delivery.finish(self.source)["covers"], job["covers"])

    def test_publication_pair_requires_both_and_cannot_mix_with_legacy_cover(self):
        pair = self.publication_pair()
        with self.assertRaisesRegex(ValueError, "both"):
            delivery.finish(self.source, cover_vertical_source=pair["cover_vertical_source"])
        with self.assertRaisesRegex(ValueError, "either"):
            delivery.finish(self.source, cover_source=self.output / "cover.png", **pair)
        self.assertFalse((self.output / "delivery.json").exists())

    def test_publication_pair_rejects_same_ratio_wrong_size_and_preserves_old_assets(self):
        pair = self.publication_pair()
        horizontal = pair["cover_horizontal_source"]
        self.images[horizontal] = {**self.images[horizontal], "width": 1280, "height": 960}
        approved = self.output / "cover-vertical.png"
        approved.write_bytes(b"previous approved cover")
        with self.assertRaisesRegex(ValueError, "1440x1080"):
            delivery.finish(self.source, overwrite=True, **pair)
        self.assertEqual(approved.read_bytes(), b"previous approved cover")
        self.assertFalse((self.output / "verification.json").exists())

    def test_publication_pair_rejects_wrong_codec_rotation_and_random_aspect_ratio(self):
        pair = self.publication_pair()
        vertical = pair["cover_vertical_source"]
        correct = self.images[vertical].copy()
        for change, message in (({"video_codec": "webp"}, "PNG or JPEG"),
                                ({"rotation": 90}, "without rotation"),
                                ({"width": 1080, "height": 1920}, "1080x1440")):
            with self.subTest(change=change):
                self.images[vertical] = {**correct, **change}
                with self.assertRaisesRegex(ValueError, message):
                    delivery.finish(self.source, **pair)

    def test_publication_pair_checks_both_destinations_before_copying(self):
        pair = self.publication_pair()
        horizontal = self.output / "cover-horizontal.jpg"
        horizontal.write_bytes(b"previous approved horizontal")
        with self.assertRaises(FileExistsError):
            delivery.finish(self.source, **pair)
        self.assertFalse((self.output / "cover-vertical.png").exists())
        self.assertEqual(horizontal.read_bytes(), b"previous approved horizontal")

    def test_publication_cover_destination_cannot_overwrite_original_through_symlink(self):
        pair = self.publication_pair()
        (self.output / "cover-vertical.png").symlink_to(self.source)
        original = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "original source"):
            delivery.finish(self.source, overwrite=True, **pair)
        self.assertEqual(self.source.read_bytes(), original)

    def test_each_publication_cover_missing_or_changed_invalidates_completed_delivery(self):
        pair = self.publication_pair()
        for role in delivery.PUBLICATION_COVERS:
            for action in ("change", "remove"):
                with self.subTest(role=role, action=action):
                    job = delivery.finish(self.source, overwrite=True, **pair)
                    path = self.output / job["covers"][role]["image"]
                    if action == "change":
                        path.write_bytes(b"changed after approval")
                    else:
                        path.unlink()
                    self.assertEqual(delivery.prepare(self.source)["status"], "rendered")

    def test_incomplete_pair_record_or_missing_video_artifact_does_not_preserve_complete(self):
        pair = self.publication_pair()
        job = delivery.finish(self.source, **pair)
        del job["covers"]["horizontal"]
        _write_json(self.output / "delivery.json", job)
        self.assertEqual(delivery.prepare(self.source)["status"], "rendered")
        delivery.finish(self.source, **pair)
        (self.output / "clean.mp4").unlink()
        self.assertEqual(delivery.prepare(self.source)["status"], "rendered")

    def test_legacy_cover_still_rejects_publication_aspect_ratio(self):
        pair = self.publication_pair()
        with self.assertRaisesRegex(ValueError, "aspect ratio does not match"):
            delivery.finish(self.source, cover_source=pair["cover_vertical_source"])

    def test_delivery_cli_passes_publication_pair_and_requires_finish(self):
        pair = self.publication_pair()
        arguments = [str(self.source), "--cover-vertical", str(pair["cover_vertical_source"]),
                     "--cover-horizontal", str(pair["cover_horizontal_source"])]
        with patch.object(delivery, "finish", return_value={}) as finish:
            self.assertEqual(delivery.main([*arguments, "--finish"]), 0)
        self.assertEqual(finish.call_args.kwargs["cover_vertical_source"], pair["cover_vertical_source"])
        self.assertEqual(finish.call_args.kwargs["cover_horizontal_source"], pair["cover_horizontal_source"])
        with self.assertRaises(SystemExit):
            delivery.main(arguments)

    def test_basic_cli_accepts_one_video_without_explicit_output(self):
        with patch.object(cli, "edit_video") as edit:
            self.assertEqual(cli.main([str(self.source)]), 0)
        self.assertEqual(edit.call_args.args[1], self.output)


if __name__ == "__main__":
    unittest.main()
