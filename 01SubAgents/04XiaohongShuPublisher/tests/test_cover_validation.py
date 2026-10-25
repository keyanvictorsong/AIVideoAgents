"""Artwork identity checks using a portrait and independently placed title."""

from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw, ImageFont, ImageOps

from xhs_publisher.cover_validation import compare_cover


def artwork(*, title_y=35, title=("INNER", "GAME")):
    picture = Image.new("RGB", (600, 800))
    draw = ImageDraw.Draw(picture)
    for y in range(800):
        draw.line((0, y, 600, y), fill=(22 + y // 20, 40 + y // 12, 82 + y // 9))
    # Keep the same subject and palette in title-layout negative examples.
    draw.ellipse((185, 250, 425, 535), fill="#bd825f")
    draw.polygon([(185, 360), (190, 265), (255, 230), (390, 250), (430, 345),
                  (350, 300), (265, 310)], fill="#302522")
    draw.ellipse((235, 365, 250, 382), fill="#201c1b")
    draw.ellipse((345, 365, 360, 382), fill="#201c1b")
    draw.arc((255, 390, 350, 465), 20, 150, fill="#693e32", width=8)
    draw.polygon([(200, 510), (405, 510), (540, 800), (60, 800)], fill="#d55235")
    draw.ellipse((365, 590, 565, 790), fill="#da8b22", outline="#51301d", width=7)
    draw.arc((400, 590, 535, 790), 70, 290, fill="#51301d", width=6)
    draw.line((365, 690, 565, 690), fill="#51301d", width=6)
    font = ImageFont.load_default(size=65)
    for row, text in enumerate(title):
        draw.text((30, title_y + row * 75), text, font=font, fill="#fff5ce", stroke_width=1)
    return picture


def encoded(picture, *, size=None, format="PNG", quality=82):
    if size:
        picture = picture.resize(size, Image.Resampling.LANCZOS)
    stream = BytesIO()
    picture.save(stream, format=format, quality=quality)
    return stream.getvalue()


class CoverValidationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.reference = artwork()
        self.path = Path(self.folder.name) / "approved.png"
        self.reference.save(self.path)

    def compare(self, picture, **options):
        return compare_cover(self.path, encoded(picture, **options))

    def test_complete_cover_survives_thumbnail_scaling_and_jpeg(self):
        for size in ((600, 800), (180, 240), (96, 128), (75, 100)):
            for format in ("PNG", "JPEG"):
                with self.subTest(size=size, format=format):
                    result = self.compare(self.reference, size=size, format=format, quality=75)
                    self.assertTrue(result["matches"], result)
                    self.assertLessEqual(result["metric"], 1)

    def test_same_subject_with_changed_title_or_layout_is_rejected(self):
        for changed in (artwork(title_y=150), artwork(title=("FIRST", "STEP"))):
            for size in ((150, 200), (75, 100)):
                with self.subTest(changed=changed, size=size):
                    result = self.compare(changed, size=size, format="JPEG", quality=75)
                    self.assertFalse(result["matches"], result)
                    self.assertGreater(result["metric"], 1)

    def test_missing_title_is_rejected_even_when_crop_is_stretched_back_to_3_to_4(self):
        cropped = self.reference.crop((0, 190, 600, 800)).resize((600, 800))
        self.assertFalse(self.compare(cropped, size=(150, 200))["matches"])

    def test_grayscale_black_and_different_video_frame_are_rejected(self):
        wrong_images = [ImageOps.grayscale(self.reference).convert("RGB"),
                        Image.new("RGB", (600, 800), "black"),
                        ImageOps.mirror(artwork(title=()))]
        for changed in wrong_images:
            with self.subTest(changed=changed):
                self.assertFalse(self.compare(changed, size=(150, 200))["matches"])

    def test_wrong_aspect_or_unreadable_preview_fails_closed(self):
        result = self.compare(self.reference, size=(180, 180))
        self.assertFalse(result["matches"])
        self.assertEqual(result["details"]["reason"], "preview_aspect_mismatch")
        self.assertFalse(compare_cover(self.path, b"not an image")["matches"])


if __name__ == "__main__":
    unittest.main()
