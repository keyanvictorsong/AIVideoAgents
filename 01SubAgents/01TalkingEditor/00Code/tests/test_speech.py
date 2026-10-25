"""Speech safety checks using fake backends, never model weights or a network."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from talking_editor import speech


def raw_segment(text="继续说话", start=31.2, end=32.4, probability=.97):
    return {
        "start": start,
        "end": end,
        "text": text,
        "words": [{"word": text, "start": start, "end": end, "probability": probability}],
    }


class SpeechNormalizationTests(unittest.TestCase):
    def test_original_timeline_and_actual_filler_text_are_preserved(self):
        source = [raw_segment("嗯，", 30.4, 30.7), raw_segment()]
        warnings = []
        words, segments = speech._normalise_segments(source, 60, warnings)
        self.assertEqual([word["id"] for word in words], [0, 1])
        self.assertEqual(words[0]["text"], "嗯，")
        self.assertEqual((words[1]["start"], words[1]["end"]), (31.2, 32.4))
        self.assertEqual((segments[1]["start"], segments[1]["end"]), (31.2, 32.4))
        self.assertEqual(source[1]["words"][0]["start"], 31.2)
        self.assertEqual(warnings, [])
        json.dumps({"words": words, "segments": segments}, allow_nan=False)

    def test_faster_whisper_objects_have_the_same_public_schema(self):
        source = SimpleNamespace(
            start=4.2, end=4.8, text="hello",
            words=[SimpleNamespace(start=4.2, end=4.8, word=" hello", probability=.9)],
        )
        words, segments = speech._normalise_segments([source], 10, [])
        self.assertEqual(words, [{"id": 0, "text": "hello", "start": 4.2, "end": 4.8, "probability": .9}])
        self.assertEqual(segments, [{"start": 4.2, "end": 4.8, "text": "hello"}])

    def test_missing_word_alignment_fails_instead_of_guessing_word_times(self):
        for words in (None, []):
            source = {"start": 0, "end": 1, "text": "不能凭空造时间", "words": words}
            with self.subTest(words=words), self.assertRaisesRegex(speech.SpeechError, "no word timestamps"):
                speech._normalise_segments([source], 10, [])

    def test_invalid_word_timestamps_fail_closed(self):
        invalid_ranges = [
            (float("nan"), 2), (1, float("inf")), (2, 1), (11, 12), (-2, -1), (None, 2),
        ]
        for start, end in invalid_ranges:
            source = raw_segment()
            source["words"][0].update(start=start, end=end)
            with self.subTest(start=start, end=end), self.assertRaisesRegex(speech.SpeechError, "invalid timestamps"):
                speech._normalise_segments([source], 10, [])

    def test_zero_duration_real_words_are_retained_and_reported(self):
        # Whisper sometimes aligns a real word to a point. Losing it could let
        # the edit planner remove real speech; expanding it would invent times.
        warnings = []
        words, _ = speech._normalise_segments([raw_segment("嗯", 2.4, 2.4)], 10, warnings)
        self.assertEqual(len(words), 1)
        self.assertEqual(words[0]["text"], "嗯")
        self.assertEqual(words[0]["start"], words[0]["end"])
        self.assertTrue(any("zero-duration" in warning for warning in warnings))

    def test_unknown_confidence_is_not_made_confident(self):
        for probability in (None, float("nan"), float("inf"), -1, 2):
            with self.subTest(probability=probability):
                words, _ = speech._normalise_segments([raw_segment(probability=probability)], 60, [])
                self.assertIsNone(words[0]["probability"])


class SpeechPublicApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audio = Path(self.directory.name) / "original.wav"
        self.audio.touch()
        self.waveform = object()
        self.patch_read = patch.object(speech, "_read_audio", return_value=(self.waveform, 60.0))
        self.patch_vad = patch.object(speech, "_speech_regions", return_value=[{"start": 30.1, "end": 33.0}])
        self.patch_backend = patch.object(speech, "_resolve_backend", return_value=("faster-whisper", "small"))
        self.patch_read.start()
        self.vad = self.patch_vad.start()
        self.patch_backend.start()
        self.addCleanup(self.patch_read.stop)
        self.addCleanup(self.patch_vad.stop)
        self.addCleanup(self.patch_backend.stop)

    def test_no_voice_never_invokes_asr_or_invents_captions(self):
        self.vad.return_value = []
        with patch.object(speech, "_transcribe_mlx") as mlx, patch.object(speech, "_transcribe_faster") as faster:
            result = speech.transcribe(self.audio)
        mlx.assert_not_called()
        faster.assert_not_called()
        self.assertEqual(result["words"], [])
        self.assertEqual(result["segments"], [])
        self.assertEqual(result["speech_regions"], [])
        self.assertEqual(result["duration"], 60)
        self.assertTrue(result["warnings"])

    def test_speech_detected_but_empty_asr_is_an_error(self):
        with patch.object(speech, "_transcribe_faster", return_value=([], "zh")):
            with self.assertRaisesRegex(speech.SpeechError, "no timed words"):
                speech.transcribe(self.audio)

    def test_original_waveform_and_vad_regions_stay_on_source_timeline(self):
        with patch.object(speech, "_transcribe_faster", return_value=([raw_segment()], "zh")) as recognize:
            result = speech.transcribe(self.audio)
        self.assertIs(recognize.call_args.args[0], self.waveform)
        self.assertEqual(result["words"][0]["start"], 31.2)
        self.assertEqual(result["speech_regions"], [{"start": 30.1, "end": 33.0}])
        self.assertEqual(result["duration"], 60)
        json.dumps(result, allow_nan=False)

    def test_vad_failure_is_not_replaced_with_asr_timestamps(self):
        self.vad.side_effect = speech.SpeechError("VAD unavailable")
        with patch.object(speech, "_transcribe_faster") as recognize:
            with self.assertRaisesRegex(speech.SpeechError, "VAD unavailable"):
                speech.transcribe(self.audio)
        recognize.assert_not_called()

    def test_auto_can_fall_back_after_mlx_failure(self):
        with (
            patch.object(speech, "_resolve_backend", return_value=("mlx", speech.DEFAULT_MLX_MODEL)),
            patch.object(speech, "_transcribe_mlx", side_effect=RuntimeError("MLX unavailable")),
            patch.object(speech, "_transcribe_faster", return_value=([raw_segment()], "zh")),
        ):
            result = speech.transcribe(self.audio)
        self.assertEqual(result["backend"], "faster-whisper")
        self.assertTrue(any("fallback" in warning for warning in result["warnings"]))

    def test_explicit_backend_or_model_failure_never_silently_changes_model(self):
        for options in ({"backend": "mlx"}, {"model": speech.DEFAULT_MLX_MODEL}):
            with (
                self.subTest(options=options),
                patch.object(speech, "_resolve_backend", return_value=("mlx", speech.DEFAULT_MLX_MODEL)),
                patch.object(speech, "_transcribe_mlx", side_effect=RuntimeError("MLX unavailable")),
                patch.object(speech, "_transcribe_faster") as faster,
            ):
                with self.assertRaisesRegex(speech.SpeechError, "MLX unavailable"):
                    speech.transcribe(self.audio, **options)
                faster.assert_not_called()


class BackendContractTests(unittest.TestCase):
    def test_faster_whisper_gets_unfiltered_audio_and_real_word_timestamps(self):
        waveform = object()
        model = Mock()
        segment = SimpleNamespace(start=31.2, end=32.4, text="你好", words=[])
        model.transcribe.return_value = (iter([segment]), SimpleNamespace(language="zh"))
        package = SimpleNamespace(WhisperModel=Mock(return_value=model))
        with patch.object(speech, "_dependency", return_value=package):
            segments, language = speech._transcribe_faster(waveform, "small", "zh", None)
        self.assertEqual(segments, [segment])
        self.assertEqual(language, "zh")
        self.assertIs(model.transcribe.call_args.args[0], waveform)
        self.assertFalse(model.transcribe.call_args.kwargs["vad_filter"])
        self.assertTrue(model.transcribe.call_args.kwargs["word_timestamps"])
        self.assertEqual(package.WhisperModel.call_args.kwargs["device"], "cpu")
        self.assertEqual(package.WhisperModel.call_args.kwargs["compute_type"], "int8")

    def test_vad_converts_sample_offsets_without_resetting_timeline(self):
        waveform = object()
        package = SimpleNamespace(
            VadOptions=lambda **kwargs: SimpleNamespace(**kwargs),
            get_speech_timestamps=Mock(return_value=[{"start": 32_000, "end": 48_000}]),
        )
        with patch.object(speech, "_dependency", return_value=package):
            regions = speech._speech_regions(waveform, 10)
        self.assertEqual(regions, [{"start": 2.0, "end": 3.0}])
        self.assertIs(package.get_speech_timestamps.call_args.args[0], waveform)


if __name__ == "__main__":
    unittest.main()
