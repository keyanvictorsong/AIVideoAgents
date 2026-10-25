"""Placement contract and actual libass pixels for future basketball tasks."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest

from talking_editor import basketball_captions, media


METADATA = {"width": 1920, "height": 1080, "video_duration": 8.0}


def cue(start=.5, end=1.8, text="好球", x=.35, y=.4):
    return {"id": "synthetic", "start": start, "end": end, "text": text,
            "evidence": {"kind": "synthetic_test_phrase", "not_real_speech": True},
            "position": {"x": x, "y": y}}


def export(plan, metadata=None):
    with tempfile.TemporaryDirectory(prefix="basketball-captions-") as directory:
        root = Path(directory)
        reviewed = basketball_captions.write_captions(plan, metadata or METADATA, root)
        return reviewed, (root / "captions.ass").read_text(), (root / "captions.srt").read_text()


class BasketballCaptionPlacementTests(unittest.TestCase):
    def test_different_cue_positions_are_preserved_without_bottom_fallback(self):
        plan = {"cues": [cue(x=.25, y=.35), cue(2.2, 3.6, "漂亮", x=.7, y=.55)]}
        reviewed, ass, _ = export(plan)
        events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(len(events), 2)
        self.assertIn(r"\an5\pos(480,378)", events[0])
        self.assertIn(r"\an5\pos(1344,594)", events[1])
        self.assertNotIn(r"\move", ass)
        self.assertEqual(reviewed[0]["layout"]["position_pixels"], [480, 378])
        self.assertFalse(reviewed[0]["layout"]["automatic_tracking"])

    def test_missing_or_malformed_position_fails_before_writing(self):
        for value in (None, {}, {"x": .5}, {"x": .5, "y": .5, "extra": 1}):
            candidate = cue()
            candidate["position"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "position"):
                export({"cues": [candidate]})
        candidate = cue()
        del candidate["position"]
        with self.assertRaisesRegex(ValueError, "position"):
            export({"cues": [candidate]})

    def test_invalid_numbers_and_normalized_bounds_are_rejected(self):
        for axis in ("x", "y"):
            for value in (float("nan"), float("inf"), -float("inf"), True, "0.5", -.01, 1.01, 10 ** 400):
                candidate = cue()
                candidate["position"][axis] = value
                with self.subTest(axis=axis, value=str(value)[:24]), self.assertRaises(ValueError):
                    export({"cues": [candidate]})

    def test_center_inside_frame_does_not_allow_the_whole_line_outside(self):
        for x, y in ((.98, .5), (.02, .5), (.5, .99), (.5, .01)):
            with self.subTest(x=x, y=y), self.assertRaisesRegex(ValueError, "whole caption"):
                export({"cues": [cue(text="这球投得非常漂亮", x=x, y=y)]})

    def test_explicit_motion_uses_both_centers_and_no_automatic_extra_motion(self):
        candidate = cue(x=.2, y=.4)
        candidate["end_position"] = {"x": .7, "y": .45}
        reviewed, ass, _ = export({"placement_mode": "near_action", "cues": [candidate]})
        self.assertIn(r"\an5\move(384,432,1344,486)", ass)
        self.assertNotIn(r"\pos", ass)
        self.assertEqual(reviewed[0]["layout"]["end_position_pixels"], [1344, 486])

    def test_motion_endpoint_must_fit_the_entire_line(self):
        for ending in ({"x": .99, "y": .5}, {"x": .5, "y": .01}, {"x": .5}, None):
            candidate = cue()
            candidate["end_position"] = ending
            with self.subTest(ending=ending), self.assertRaisesRegex(ValueError, "end_position"):
                export({"cues": [candidate]})

    def test_srt_contains_only_normal_text_and_timing(self):
        _, ass, srt = export({"cues": [cue()]})
        self.assertEqual(srt, "1\n00:00:00,500 --> 00:00:01,800\n好球\n")
        self.assertNotIn("\\", srt)
        self.assertNotIn("position", srt)
        self.assertIn(r"\pos", ass)

    def test_short_source_text_cannot_inject_ass_overrides(self):
        _, ass, _ = export({"cues": [cue(text=r"{\b1}好球")]})
        events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(len(events), 1)
        self.assertNotIn(r"{\b1}", events[0])
        self.assertIn("｛＼b1｝好球", events[0])
        self.assertEqual(events[0].count("{"), 1)

    def test_font_or_text_cannot_add_ass_lines_or_fields(self):
        for font in ("Font,99", "Font\rStyle: fake", "Font\\pos", "Font{override}"):
            with self.subTest(font=font), self.assertRaisesRegex(ValueError, "font"):
                export({"style": {"font": font}, "cues": [cue()]})
        with self.assertRaisesRegex(ValueError, "one short"):
            export({"cues": [cue(text="好\n球")]})

    def test_unknown_mode_cannot_silently_use_legacy_bottom_layout(self):
        for mode in ("bottom", "automatic_tracking", None):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "placement_mode"):
                export({"placement_mode": mode, "cues": [cue()]})

    def test_source_evidence_time_and_ass_export_precision_are_enforced(self):
        candidates = []
        missing_evidence = cue()
        del missing_evidence["evidence"]
        candidates.append(missing_evidence)
        candidates.extend([cue(-.1, .5), cue(7.5, 8.1), cue(.001, .004), cue(1, 1)])
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                export({"cues": [candidate]})
        with self.assertRaisesRegex(ValueError, "overlaps"):
            export({"cues": [cue(), cue(1, 2)]})

    def test_white_and_yellow_cues_retain_brief_fades(self):
        accented = cue(2, 3, "漂亮", x=.65)
        accented["accent"] = True
        _, ass, _ = export({"cues": [cue(), accented]})
        events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        self.assertIn(r"\c&HFFFFFF&\fad(80,80)", events[0])
        self.assertIn(r"\c&H3DD8FF&\fad(80,80)", events[1])
        with self.assertRaisesRegex(ValueError, "maximum 80"):
            export({"style": {"fade_ms": 81}, "cues": [cue()]})

    def test_invalid_color_and_accent_values_produce_clear_errors(self):
        for key, value in (("color", []), ("color", "pink"), ("accent", "true")):
            candidate = cue()
            candidate[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                export({"cues": [candidate]})


class BasketballCaptionLibassSmokeTests(unittest.TestCase):
    def test_actual_pixels_follow_two_manual_positions_on_black_frames(self):
        try:
            ffmpeg = media._binary("ffmpeg")
        except media.MediaError as exc:
            self.skipTest(str(exc))
        self.assertIn("ass", media._filters(ffmpeg), "FFmpeg must contain libass for this smoke test")
        evidence = tempfile.TemporaryDirectory(prefix="basketball-caption-smoke-")
        self.addCleanup(evidence.cleanup)
        evidence_dir = Path(evidence.name)
        plan = {
            "example_only": True, "placement_mode": "near_action",
            "style": {"font_size": 28, "outline": 1, "shadow": 0, "fade_ms": 0},
            "cues": [cue(0, .8, x=.25, y=.35), cue(1, 1.8, "漂亮", x=.72, y=.55)]}
        metadata = {"width": 640, "height": 360, "video_duration": 2.0}
        basketball_captions.write_captions(plan, metadata, evidence_dir)
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-f", "lavfi", "-i", "color=c=black:s=640x360:r=1:d=2",
                   "-vf", "ass=filename=captions.ass,bbox=min_val=40,metadata=mode=print:file=-",
                   "-frames:v", "2", "-f", "null", "-"]
        metadata_text = media._run(command, cwd=evidence_dir)
        boxes = []
        for block in re.split(r"(?=frame:\s*\d+)", metadata_text):
            values = dict(re.findall(r"lavfi\.bbox\.(x1|x2|y1|y2)=(\d+)", block))
            if len(values) == 4:
                boxes.append({key: int(value) for key, value in values.items()})
        expected = [(160, 126), (460.8, 198)]
        centers = [((b["x1"] + b["x2"]) / 2, (b["y1"] + b["y2"]) / 2) for b in boxes]
        matches = len(centers) == len(expected) and all(
            abs(found[0] - target[0]) <= 21 and abs(found[1] - target[1]) <= 21
            for found, target in zip(centers, expected))
        result = {"checked_at": datetime.now(timezone.utc).isoformat(), "synthetic_only": True,
                  "passed": matches, "expected_centers": expected, "actual_bounding_boxes": boxes,
                  "actual_glyph_centers": centers, "center_tolerance_pixels": 21,
                  "bottom_layout_would_fail": True, "argv": command,
                  "font_note": "Glyph ink can differ from the ASS line-box center; tolerance is 0.75 of the 28 px test font.",
                  "runtime_compatibility_notes": list(media._runtime_compatibility_notes)}
        (evidence_dir / "smoke-plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        (evidence_dir / "bbox-metadata.txt").write_text(metadata_text)
        (evidence_dir / "smoke-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(len(boxes), 2, metadata_text)
        self.assertTrue(matches, result)
        self.assertLess(max(y for _, y in centers), metadata["height"] * .75)


if __name__ == "__main__":
    unittest.main()
