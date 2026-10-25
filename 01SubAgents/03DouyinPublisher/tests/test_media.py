from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from douyin_agent.media import (
    MediaError,
    _analyze_frame,
    _detect_existing_title,
    _draw_title,
    _fit_protected,
    _resolve_font,
    _sample_times,
    _subtitle_likelihood,
    _tone_map_hdr,
    _wrap_text,
    prepare_covers,
)


class CoverLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.font = _resolve_font(None)
        except MediaError as exc:
            raise unittest.SkipTest(str(exc))

    def test_title_splits_at_colon_without_losing_characters(self):
        font = ImageFont.truetype(self.font, 70)
        title = "学习像旅行：要认真思考"
        lines = _wrap_text(title, font, 900)
        self.assertEqual(lines, ["学习像旅行：", "要认真思考"])
        self.assertEqual("".join(lines), title)

    def test_titles_are_never_inferred_from_color_or_an_original_style(self):
        original = Image.new("RGB", (720, 1280), "#808080")
        titled, info = _draw_title(original, "学习像旅行：要认真思考", self.font, [])
        self.assertEqual(info["lines"], ["学习像旅行：", "要认真思考"])
        detection = _detect_existing_title(titled)
        self.assertFalse(detection["detected"], detection)
        self.assertFalse(_detect_existing_title(Image.new("RGB", (720, 1280), "yellow"))["detected"])
        self.assertFalse(_detect_existing_title(original)["detected"])

    def test_cover_preserves_faces_and_top_title_in_vertical_crop(self):
        frame = Image.new("RGB", (900, 1600), "#556677")
        face = [[440, 650, 800, 1140]]
        title = [200, 150, 700, 340]
        cover, faces, layout = _fit_protected(frame, (1080, 1440), face, title)
        self.assertEqual(cover.size, (1080, 1440))
        self.assertIn("裁切", layout)
        self.assertGreaterEqual(min(faces[0]), 0)
        self.assertLessEqual(faces[0][2], 1080)
        self.assertLessEqual(faces[0][3], 1440)

    def test_horizontal_portrait_uses_entire_frame_and_blurred_background(self):
        frame = Image.new("RGB", (900, 1600), "#556677")
        cover, faces, layout = _fit_protected(frame, (1440, 1080), [[400, 700, 800, 1200]], None)
        self.assertEqual(cover.size, (1440, 1080))
        self.assertIn("完整画面", layout)
        self.assertGreater(faces[0][0], 300)
        self.assertLess(faces[0][2], 1100)

    def test_preserve_unknown_title_does_not_crop_top_or_bottom(self):
        frame = Image.new("RGB", (900, 1600), "#556677")
        cover, _, layout = _fit_protected(frame, (1080, 1440), [], None, preserve_frame=True)
        self.assertEqual(cover.size, (1080, 1440))
        self.assertIn("完整画面", layout)

    def test_hdr_transforms_are_finite_monotonic_and_keep_neutral_pixels_neutral(self):
        ramp = np.repeat(np.linspace(0., 1., 256, dtype=np.float32)[None, :, None], 3, axis=2)
        for transfer in ("arib-std-b67", "smpte2084"):
            converted = np.asarray(_tone_map_hdr(ramp, transfer, "bt2020"))
            self.assertEqual(converted.dtype, np.uint8)
            self.assertEqual(int(converted[0, 0, 0]), 0)
            self.assertEqual(int(converted[0, -1, 0]), 255)
            self.assertTrue(np.all(np.diff(converted[0, :, 0].astype(int)) >= 0))
            self.assertLessEqual(int(np.max(np.abs(converted[:, :, 0].astype(int) - converted[:, :, 1].astype(int)))), 1)

    def test_title_moves_away_from_top_face(self):
        frame = Image.new("RGB", (720, 1280), "#808080")
        _, info = _draw_title(frame, "学习像旅行：要认真思考", self.font, [[120, 50, 650, 480]])
        self.assertGreater(info["y"], 480)
        self.assertEqual(info["face_overlap_pixels"], 0)

    def test_sampling_includes_early_title_and_never_end(self):
        times = _sample_times(180., 16)
        self.assertEqual(len(times), 16)
        self.assertIn(2., times)
        self.assertIn(4., times)
        self.assertTrue(all(0 <= t < 180 for t in times))
        tiny = _sample_times(.01, 16)
        self.assertTrue(all(0 <= t < .01 for t in tiny))

    def test_sharp_well_exposed_frame_scores_above_blank_black_frame(self):
        # Spatial detail and exposure matter independently of scene meaning.
        grid = np.indices((320, 240)).sum(axis=0) // 8 % 2 * 180 + 35
        detailed = Image.fromarray(np.repeat(grid.astype(np.uint8)[:, :, None], 3, axis=2))
        black = Image.new("RGB", (240, 320), "black")
        self.assertGreater(_analyze_frame(detailed)["score"], _analyze_frame(black)["score"])

    def test_subtitle_line_is_detected_but_large_white_rectangle_is_not(self):
        frame = Image.new("RGB", (640, 1138), "#69717c")
        captioned = frame.copy()
        draw = ImageDraw.Draw(captioned)
        font = ImageFont.truetype(self.font, 28)
        draw.text((60, 840), "能不能找到一个middle ground呢", font=font,
                  fill="white", stroke_width=2, stroke_fill="black")
        draw.text((60, 840), "能不能", font=font,
                  fill="#fff32a", stroke_width=2, stroke_fill="black")
        caption_detection = _subtitle_likelihood(captioned)
        self.assertGreater(caption_detection["likelihood"], .8, caption_detection)
        self.assertGreaterEqual(caption_detection["components"], 6)
        rectangle = frame.copy()
        ImageDraw.Draw(rectangle).rectangle((60, 840, 580, 875), fill="white", outline="black", width=2)
        self.assertEqual(_subtitle_likelihood(rectangle)["likelihood"], 0.)
        self.assertEqual(_subtitle_likelihood(frame)["likelihood"], 0.)

    def test_short_chinese_subtitle_still_receives_a_moderate_penalty(self):
        frame = Image.new("RGB", (640, 1138), "#69717c")
        draw = ImageDraw.Draw(frame)
        font = ImageFont.truetype(self.font, 36)
        draw.text((248, 865), "看示例呢", font=font,
                  fill="white", stroke_width=2, stroke_fill="black")
        draw.text((248, 865), "看示例", font=font,
                  fill="#fff32a", stroke_width=2, stroke_fill="black")
        detection = _subtitle_likelihood(frame)
        self.assertGreater(detection["likelihood"], .35, detection)
        self.assertGreaterEqual(detection["components"], 3)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class CoverVideoIntegrationTests(unittest.TestCase):
    def test_real_video_with_unicode_path_generates_covers_and_preserves_burned_title(self):
        try:
            font = _resolve_font(None)
        except MediaError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory(prefix="douyin media ") as temporary:
            folder = Path(temporary)
            source, video, output = folder / "原图 含标题.png", folder / "示例 测试.mp4", folder / "封面 输出"
            image, _ = _draw_title(Image.new("RGB", (360, 640), "#69717c"), "学习像旅行：要认真思考", font, [])
            image.save(source)
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(source),
                "-t", "1", "-r", "5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
            ], check=True, capture_output=True)
            result = prepare_covers(video, "学习像旅行：要认真思考", output, samples=2)
            json.dumps(result, ensure_ascii=False)
            self.assertTrue(result["title_preserved"], result["title_detection"])
            self.assertFalse(result["hdr_tone_mapped"])
            for kind, expected in (("native", (360, 640)), ("vertical", (1080, 1440)), ("horizontal", (1440, 1080))):
                path = Path(result[kind])
                self.assertTrue(path.is_absolute())
                with Image.open(path) as rendered:
                    self.assertEqual(rendered.size, expected)
                self.assertIsNone(result["layouts"][kind]["title"])
            self.assertTrue(Path(result["raw_frame"]).is_file())
            self.assertTrue(Path(result["contact_sheet"]).is_file())
            with self.assertRaisesRegex(MediaError, "frame_time"):
                prepare_covers(video, "测试", folder / "invalid", frame_time=1.)
            self.assertFalse((folder / "invalid").exists())


if __name__ == "__main__":
    unittest.main()
