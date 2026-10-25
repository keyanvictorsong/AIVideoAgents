"""Pipeline boundaries using small file fixtures, without FFmpeg or ASR models."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from talking_editor import media, pipeline, speech


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "raw"
        self.inputs.mkdir()
        self.source = self.inputs / "sample.MOV"
        self.source.write_bytes(b"unique source bytes")
        self.output = self.root / "outputs" / "sample"
        self.config = {"backend": "faster-whisper", "model": "test-model", "captions": {"highlights": False}}
        self.calls = {}
        for target, name, function in (
            (media, "probe", self.fake_probe),
            (media, "extract_audio", self.fake_extract),
            (media, "render", self.fake_render),
            (media, "burn_subtitles", self.fake_burn),
            (speech, "transcribe", self.fake_transcribe),
        ):
            patcher = patch.object(target, name, side_effect=function)
            self.calls[name] = patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def fake_probe(source):
        if "broken" in Path(source).stem:
            raise RuntimeError("Unreadable video fixture")
        return {"duration": 5.0, "width": 640, "height": 360, "has_audio": True}

    @staticmethod
    def fake_extract(source, destination):
        Path(destination).write_bytes(b"derived audio")

    @staticmethod
    def fake_transcribe(audio, **kwargs):
        return {"schema_version": 1, "backend": kwargs["backend"], "model": kwargs["model"],
                "words": [{"id": 0, "text": "你好世界", "start": 0, "end": 5, "probability": .99}],
                "segments": [{"start": 0, "end": 5, "text": "你好世界"}],
                "speech_regions": [{"start": 0, "end": 5}], "warnings": []}

    @staticmethod
    def fake_render(source, destination, keep, ass_path=None, options=None):
        destination = Path(destination)
        if destination.exists() and not (options or {}).get("overwrite", False):
            raise RuntimeError("Renderer requires overwrite=True for existing output")
        destination.write_bytes(b"rendered clean or captioned video")
        return {"duration": keep[-1]["output_end"], "width": 640, "height": 360}

    @staticmethod
    def fake_burn(source, destination, ass_path, options=None):
        destination = Path(destination)
        if destination.exists() and not (options or {}).get("overwrite", False):
            raise RuntimeError("Renderer requires overwrite=True for existing output")
        destination.write_bytes(b"rendered captioned video")
        return {"duration": 5, "width": 640, "height": 360}

    def edit(self, **kwargs):
        config = kwargs.pop("config", self.config)
        return pipeline.edit_video(self.source, self.output, config=config, **kwargs)

    def test_identical_complete_request_skips_all_expensive_work(self):
        first = self.edit()
        probe_calls = self.calls["probe"].call_count
        second = self.edit()
        self.assertEqual(first["status"], "complete")
        self.assertTrue(second["skipped"])
        self.assertEqual(self.calls["transcribe"].call_count, 1)
        self.assertEqual(self.calls["render"].call_count, 1)
        self.assertEqual(self.calls["probe"].call_count, probe_calls)

    def test_analysis_and_caption_changes_reuse_original_transcript(self):
        self.edit(analyze_only=True)
        changed = copy.deepcopy(self.config)
        changed["captions"]["max_line_chars"] = 12
        self.edit(config=changed, analyze_only=True)
        self.assertEqual(self.calls["transcribe"].call_count, 1)
        self.assertEqual(self.calls["extract_audio"].call_count, 1)

    def test_overwrite_propagates_to_both_renderers_and_reuses_asr(self):
        self.edit()
        changed = copy.deepcopy(self.config)
        changed["captions"]["max_line_chars"] = 12
        result = self.edit(config=changed, overwrite=True)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.calls["transcribe"].call_count, 1)
        self.assertTrue(self.calls["render"].call_args.kwargs["options"]["overwrite"])
        self.assertTrue(self.calls["burn_subtitles"].call_args.kwargs["options"]["overwrite"])

    def test_source_content_change_invalidates_transcript_cache(self):
        first = self.edit(analyze_only=True)
        self.source.write_bytes(b"different recording with the same filename")
        second = self.edit(analyze_only=True)
        self.assertNotEqual(first["source_sha256"], second["source_sha256"])
        self.assertEqual(self.calls["transcribe"].call_count, 2)

    def test_model_change_invalidates_transcript_cache(self):
        self.edit(analyze_only=True)
        changed = copy.deepcopy(self.config)
        changed["model"] = "another-model"
        self.edit(config=changed, analyze_only=True)
        self.assertEqual(self.calls["transcribe"].call_count, 2)

    def test_changed_request_requires_overwrite_and_preserves_existing_files(self):
        self.edit()
        original_plan = (self.output / "edit-plan.json").read_bytes()
        original_video = (self.output / "edited.mp4").read_bytes()
        changed = copy.deepcopy(self.config)
        changed["planner"] = {"remove_filler_audio": False}
        with self.assertRaises(FileExistsError):
            self.edit(config=changed)
        self.assertEqual((self.output / "edit-plan.json").read_bytes(), original_plan)
        self.assertEqual((self.output / "edited.mp4").read_bytes(), original_video)
        self.assertEqual(self.calls["transcribe"].call_count, 1)

    def test_reviewed_plan_from_another_source_is_rejected_before_analysis(self):
        reviewed = self.root / "reviewed.json"
        reviewed.write_text(json.dumps({"source_sha256": "different source"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "different source"):
            self.edit(plan_path=reviewed, analyze_only=True)
        self.assertFalse(self.output.exists())
        self.calls["probe"].assert_not_called()
        self.calls["transcribe"].assert_not_called()

    def test_inconsistent_reviewed_duration_is_rejected_before_writing_products(self):
        self.edit(analyze_only=True)
        plan = json.loads((self.output / "edit-plan.json").read_text(encoding="utf-8"))
        plan["output_duration"] = 1.0  # keep still maps the full five seconds.
        reviewed = self.root / "reviewed.json"
        reviewed.write_text(json.dumps(plan), encoding="utf-8")
        destination = self.root / "reviewed-output"
        with self.assertRaises((ValueError, RuntimeError)):
            pipeline.edit_video(self.source, destination, config=self.config, plan_path=reviewed, analyze_only=True)
        self.assertFalse((destination / "edit-plan.json").exists())

    def test_batch_continues_after_single_video_failure(self):
        (self.inputs / "a-broken.mov").write_bytes(b"broken fixture")
        (self.inputs / "notes.txt").write_text("ignore me", encoding="utf-8")
        result = pipeline.edit_folder(self.inputs, self.root / "batch", config=self.config, analyze_only=True)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertEqual(result["results"][1]["status"], "analyzed")
        self.assertTrue((self.root / "batch" / "batch-report.json").is_file())

    def test_same_stem_batch_collision_is_rejected(self):
        (self.inputs / "sample.mp4").write_bytes(b"another source")
        with self.assertRaisesRegex(ValueError, "unique stems"):
            pipeline.edit_folder(self.inputs, self.root / "batch", config=self.config)
        self.calls["transcribe"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
