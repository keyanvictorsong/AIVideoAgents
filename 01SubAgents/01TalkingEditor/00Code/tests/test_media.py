"""Integration checks for the FFmpeg boundary; all footage is synthetic."""

import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from talking_editor import delivery, media
from talking_editor.media import MediaError, _binary, _normalise_keep, _run, extract_audio, probe, render


class TimelineValidationTests(unittest.TestCase):
    def test_rejects_overlaps_and_output_gaps(self):
        with self.assertRaises(MediaError):
            _normalise_keep([{"source_start": 0, "source_end": 2},
                             {"source_start": 1, "source_end": 3}], 5)
        with self.assertRaises(MediaError):
            _normalise_keep([{"source_start": 0, "source_end": 2,
                              "output_start": 1, "output_end": 3}], 5)

    def test_tonemap_desaturation_rejects_nonfinite_negative_and_nonnumeric_values(self):
        for value in (-0.1, float("nan"), float("inf"), True, None, "invalid", 10**400):
            with self.subTest(value=value), self.assertRaisesRegex(MediaError, "finite nonnegative"):
                media._tonemap_desat({"tonemap_desat": value})
        self.assertEqual(media._tonemap_desat(), 2)


class RuntimeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.library_patch = patch.object(media, "_runtime_library_path", None)
        self.notes_patch = patch.object(media, "_runtime_compatibility_notes", [])
        self.library_patch.start()
        self.notes_patch.start()
        self.addCleanup(self.library_patch.stop)
        self.addCleanup(self.notes_patch.stop)

    def test_loader_fallback_is_scoped_and_reused_after_success(self):
        failure = subprocess.CompletedProcess([], -6, "", "dyld: freetype Library not loaded: /usr/lib/libhvf.dylib")
        success = subprocess.CompletedProcess([], 0, "works", "")
        with patch.object(media, "_freetype_candidates", return_value=[Path("/test/2.14/lib")]), \
                patch.object(media.subprocess, "run", side_effect=[failure, success, success]) as run:
            self.assertEqual(_run(["ffmpeg", "-version"]), "works")
            self.assertEqual(_run(["ffprobe", "-version"]), "works")
        self.assertIsNone(run.call_args_list[0].kwargs["env"])
        for call in run.call_args_list[1:]:
            self.assertTrue(call.kwargs["env"]["DYLD_LIBRARY_PATH"].startswith("/test/2.14/lib"))
        self.assertIn("subprocess-only", media._runtime_compatibility_notes[0])

    def test_regular_media_errors_never_trigger_loader_retry(self):
        failure = subprocess.CompletedProcess([], 1, "", "Input file is invalid")
        with patch.object(media, "_freetype_candidates") as candidates, \
                patch.object(media.subprocess, "run", return_value=failure) as run:
            with self.assertRaisesRegex(MediaError, "Input file is invalid"):
                _run(["ffmpeg", "-i", "invalid.mp4"])
        candidates.assert_not_called()
        self.assertEqual(run.call_count, 1)

    def test_missing_compatible_library_has_actionable_error(self):
        failure = subprocess.CompletedProcess([], -6, "", "dyld: freetype Library not loaded: libhvf.dylib")
        with patch.object(media, "_freetype_candidates", return_value=[]), \
                patch.object(media.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(MediaError, "No working retained FreeType keg"):
                _run(["ffmpeg", "-version"])

    def test_filter_capabilities_support_current_two_flag_format(self):
        media._filters.cache_clear()
        with patch.object(media, "_run", return_value=" .. ass V->V Render\n .S zscale V->V Scale\n TS tonemap V->V Map\n"):
            self.assertEqual(media._filters("synthetic-binary"), frozenset({"ass", "zscale", "tonemap"}))
        media._filters.cache_clear()


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for media integration checks")
class MediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="talking-media-test-")
        cls.directory = Path(cls.temporary.name)
        cls.source = cls.directory / "source.mp4"
        _run([
            _binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=9",
            "-f", "lavfi", "-i", "sine=sample_rate=48000:duration=9",
            "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-c:a", "aac", str(cls.source),
        ])

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_many_fractional_cuts_do_not_accumulate_av_drift(self):
        keep, cursor = [], 0.0
        # At 10 fps, independently rounding each .1379s clip would lose over
        # one second across these cuts. The cumulative timeline stays exact.
        for index in range(31):
            keep.append({"source_start": index*.25, "source_end": index*.25+.1379,
                         "output_start": cursor, "output_end": cursor+.1379})
            cursor += .1379
        before = hashlib.sha256(self.source.read_bytes()).digest()
        result = render(self.source, self.directory / "很多剪辑 apostrophe's, output.mp4", keep,
                        options={"preset": "ultrafast", "threads": 1})
        self.assertLess(abs(result["audio_duration"]-cursor), .002)
        self.assertLessEqual(abs(result["video_duration"]-cursor), .1)
        self.assertEqual((result["width"], result["height"]), (160, 90))
        self.assertEqual(result["color_transfer"], "bt709")
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).digest())

    def test_portrait_is_physically_rotated_and_metadata_cleared(self):
        rotated = self.directory / "rotated.mov"
        _run([
            _binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-display_rotation:v:0", "90", "-i", str(self.source), "-map", "0", "-c", "copy", str(rotated),
        ])
        self.assertEqual((probe(rotated)["width"], probe(rotated)["height"]), (90, 160))
        result = render(rotated, self.directory / "portrait.mp4",
                        [{"source_start": 1, "source_end": 2.5}],
                        options={"preset": "ultrafast", "threads": 1})
        self.assertEqual((result["width"], result["height"]), (90, 160))
        self.assertEqual(result["rotation"], 0)

    def test_audio_extraction_matches_asr_contract(self):
        destination = self.directory / "speech.wav"
        extract_audio(self.source, destination)
        with wave.open(str(destination), "rb") as audio:
            self.assertEqual(audio.getframerate(), 16000)
            self.assertEqual(audio.getnchannels(), 1)
            self.assertEqual(audio.getsampwidth(), 2)
            self.assertAlmostEqual(audio.getnframes()/16000, 9, delta=.03)

    def test_hdr_color_preserving_option_survives_render_and_cover_extraction(self):
        if not {"zscale", "tonemap"} <= media._filters(_binary("ffmpeg")):
            self.skipTest("HDR conversion requires zscale and tonemap")
        source = self.directory / "synthetic-hlg-highlight.mp4"
        # This is an encoded HLG test chart, not a relabelled user recording.
        # Its bright warm patch exercises highlight desaturation, where the
        # historical default makes strong chroma approach neutral white.
        _run([
            _binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "color=c=0xE0A080:size=64x64:rate=10:duration=0.6",
            "-vf", "format=yuv420p10le,setparams=range=limited:color_primaries=bt2020:"
                   "color_trc=arib-std-b67:colorspace=bt2020nc",
            "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", "-crf", "0",
            "-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
            "-colorspace", "bt2020nc", "-color_range", "tv", str(source),
        ])
        self.assertTrue(probe(source)["hdr"])

        def mean_rgb(path):
            raw = path.with_suffix(".rgb")
            _run([_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
                  "-i", str(path), "-frames:v", "1", "-vf", "format=rgb24", "-f", "rawvideo", str(raw)])
            pixels = raw.read_bytes()
            self.assertEqual(len(pixels), 64 * 64 * 3)
            return [sum(pixels[channel::3]) / (64 * 64) for channel in range(3)]

        colors = []
        for name, options in (("default", {}), ("preserved", {"tonemap_desat": 0})):
            target = self.directory / f"hlg-{name}.mp4"
            result = render(source, target, [{"source_start": 0, "source_end": .6}],
                            options={"preset": "ultrafast", "threads": 1, **options})
            self.assertFalse(result["hdr"])
            self.assertTrue(result["tone_mapped"])
            self.assertEqual((result["color_transfer"], result["color_primaries"],
                              result["color_space"], result["color_range"]),
                             ("bt709", "bt709", "bt709", "tv"))
            colors.append(mean_rgb(target))
        default, preserved = colors
        self.assertGreater(max(preserved) - min(preserved), max(default) - min(default) + 70)
        self.assertGreater(preserved[0], preserved[1] * 1.5)
        cover = self.directory / "hlg-preserved-cover.jpg"
        delivery.extract_cover_frame(source, cover, .2, options={"tonemap_desat": 0})
        for actual, expected in zip(mean_rgb(cover), preserved):
            self.assertAlmostEqual(actual, expected, delta=8)

    def test_cannot_overwrite_original(self):
        with self.assertRaisesRegex(MediaError, "original recordings"):
            render(self.source, self.source, [{"source_start": 0, "source_end": 1}],
                   options={"overwrite": True})


if __name__ == "__main__":
    unittest.main()
