"""Behavioral checks for protecting speech while making reversible edits."""

import copy
import math
import unittest

from talking_editor.planner import build_plan


def word(identifier, text, start, end, probability=.99):
    return {"id": identifier, "text": text, "start": start, "end": end, "probability": probability}


def transcript(words, regions=None):
    result = {"words": words, "segments": []}
    if regions is not None:
        result["speech_regions"] = [{"start": start, "end": end} for start, end in regions]
    return result


class PlannerTests(unittest.TestCase):
    def assert_mapping(self, plan):
        output_cursor = 0
        source_cursor = 0
        for keep in plan["keep"]:
            self.assertGreaterEqual(keep["source_start"], source_cursor)
            self.assertGreater(keep["source_end"], keep["source_start"])
            self.assertAlmostEqual(keep["output_start"], output_cursor)
            self.assertAlmostEqual(keep["source_end"] - keep["source_start"], keep["output_end"] - keep["output_start"])
            source_cursor = keep["source_end"]
            output_cursor = keep["output_end"]
        self.assertAlmostEqual(plan["output_duration"], output_cursor)
        self.assertAlmostEqual(plan["source_duration"], output_cursor + sum(c["end"] - c["start"] for c in plan["removed"]))

    def test_missing_vad_never_interprets_asr_gaps_as_silence(self):
        plan = build_plan(transcript([word(0, "嗯", .2, .4), word(1, "你好", 8, 9)]), 10)
        self.assertEqual(plan["removed"], [])
        self.assertEqual(plan["output_duration"], 10)
        self.assertTrue(plan["fillers"][0]["subtitle_hidden"])
        self.assertFalse(plan["fillers"][0]["audio_removed"])
        self.assertTrue(plan["warnings"])
        self.assert_mapping(plan)

    def test_silence_compression_and_mapping(self):
        plan = build_plan(transcript([word(0, "第一句", 2, 4), word(1, "第二句", 7, 9)], [(2, 4), (7, 9)]), 12)
        self.assertEqual(len(plan["removed"]), 3)
        self.assertAlmostEqual(plan["removed"][1]["start"], 4.225)
        self.assertAlmostEqual(plan["removed"][1]["end"], 6.775)
        self.assertAlmostEqual(plan["output_duration"], 4.73)
        self.assert_mapping(plan)

    def test_words_protect_speech_when_vad_is_shorter(self):
        words = [word(0, "你好", 1, 3), word(1, "还在说话", 3.1, 5), word(2, "下一句", 8, 9)]
        plan = build_plan(transcript(words, [(1, 2), (8, 9)]), 10)
        for cut in plan["removed"]:
            for item in words:
                self.assertFalse(cut["start"] < item["end"] and cut["end"] > item["start"])
        self.assert_mapping(plan)

    def test_obvious_isolated_filler_removed_from_audio_and_subtitles(self):
        words = [word(0, "第一句话", 0, 2), word(1, "嗯，", 2.6, 2.9), word(2, "呃", 3.0, 3.2), word(3, "第二句话", 3.8, 6)]
        source = transcript(words, [(0, 2), (2.55, 3.25), (3.8, 6)])
        original = copy.deepcopy(source)
        plan = build_plan(source, 6)
        self.assertEqual(source, original)
        self.assertEqual(len(plan["fillers"]), 1)
        filler = plan["fillers"][0]
        self.assertEqual(filler["word_ids"], [1, 2])
        self.assertTrue(filler["subtitle_hidden"])
        self.assertTrue(filler["audio_removed"])
        self.assertEqual(len(plan["removed"]), 1)
        self.assert_mapping(plan)

    def test_filler_inside_continuous_sentence_only_hidden(self):
        words = [word(0, "我觉得", 0, 1), word(1, "嗯", 1, 1.2), word(2, "这样很好", 1.2, 3)]
        plan = build_plan(transcript(words, [(0, 3)]), 3)
        self.assertEqual(plan["removed"], [])
        self.assertTrue(plan["fillers"][0]["subtitle_hidden"])
        self.assertFalse(plan["fillers"][0]["audio_removed"])

    def test_ambiguous_content_negations_and_numbers_are_kept(self):
        tokens = ["那个", "这个", "然后", "就是", "不", "没有", "100", "唔"]
        words = [word(i, token, i, i + .9) for i, token in enumerate(tokens)]
        plan = build_plan(transcript(words, [(0, 8)]), 8)
        self.assertEqual(plan["fillers"], [])
        self.assertEqual(plan["removed"], [])

    def test_unknown_or_low_probability_cannot_remove_filler_audio(self):
        for probability in (None, .4):
            words = [word(0, "Hi", 0, 2), word(1, "um", 2.5, 2.8, probability), word(2, "there", 3.3, 5)]
            plan = build_plan(transcript(words, [(0, 2), (2.5, 2.8), (3.3, 5)]), 5)
            self.assertTrue(plan["fillers"][0]["subtitle_hidden"])
            self.assertFalse(plan["fillers"][0]["audio_removed"])

    def test_short_spoken_fragments_prevent_a_cut(self):
        words = [word(0, "好", 0, .3), word(1, "嗯", .7, .9), word(2, "继续说话", 1.3, 4)]
        plan = build_plan(transcript(words, [(0, .3), (.7, .9), (1.3, 4)]), 4)
        self.assertEqual(plan["removed"], [])
        self.assertFalse(plan["fillers"][0]["audio_removed"])
        self.assertTrue(plan["fillers"][0]["subtitle_hidden"])

    def test_cut_density_limits_staccato_output(self):
        words = [word(i, "说话", i * 2, i * 2 + 1) for i in range(6)]
        plan = build_plan(transcript(words, [(w["start"], w["end"]) for w in words]), 11, {"max_cuts_per_window": 2, "cut_window": 20})
        self.assertEqual(len(plan["removed"]), 2)
        self.assertTrue(plan["warnings"])
        self.assert_mapping(plan)

    def test_remove_fillers_can_be_disabled(self):
        plan = build_plan(transcript([word(0, "嗯", 0, 1)], [(0, 1)]), 1, {"remove_fillers": False})
        self.assertFalse(plan["fillers"][0]["subtitle_hidden"])
        self.assertFalse(plan["fillers"][0]["audio_removed"])
        self.assertEqual(plan["output_duration"], 1)

    def test_filler_audio_can_be_preserved_while_subtitles_are_cleaned(self):
        words = [word(0, "Hello", 0, 2), word(1, "um", 2.6, 2.9), word(2, "world", 3.5, 5)]
        plan = build_plan(transcript(words, [(0, 2), (2.6, 2.9), (3.5, 5)]), 5, {"remove_filler_audio": False})
        self.assertTrue(plan["fillers"][0]["subtitle_hidden"])
        self.assertFalse(plan["fillers"][0]["audio_removed"])
        self.assertEqual(plan["removed"], [])

    def test_zero_duration_word_protects_its_entire_asr_segment(self):
        source = transcript([word(0, "说话", .5, 1), word(1, "重要", 4, 4), word(2, "继续", 7.5, 8)], [(.5, 1), (7.5, 8)])
        source["segments"] = [{"start": 1, "end": 7, "text": "重要"}]
        plan = build_plan(source, 9)
        for cut in plan["removed"]:
            self.assertFalse(cut["start"] < 7 and cut["end"] > 1)
        self.assertTrue(any("Zero-duration" in warning for warning in plan["warnings"]))
        self.assert_mapping(plan)

    def test_zero_duration_filler_does_not_supply_an_audio_cut(self):
        source = transcript([word(0, "Hello", 0, 2), word(1, "um", 2.6, 2.6), word(2, "world", 3.5, 5)], [(0, 2), (2.5, 2.9), (3.5, 5)])
        source["segments"] = [{"start": 2.4, "end": 3.2, "text": "um"}]
        plan = build_plan(source, 5)
        self.assertTrue(plan["fillers"][0]["subtitle_hidden"])
        self.assertFalse(plan["fillers"][0]["audio_removed"])
        self.assertEqual(plan["removed"], [])

    def test_unanchored_zero_duration_word_retains_original_audio(self):
        plan = build_plan(transcript([word(0, "重要", 4, 4)], [(1, 2), (7, 8)]), 10)
        self.assertEqual(plan["removed"], [])
        self.assertEqual(plan["output_duration"], 10)

    def test_zero_duration_segment_cannot_anchor_zero_duration_word(self):
        source = transcript([word(0, "重要", 4, 4)], [(1, 2), (7, 8)])
        source["segments"] = [{"start": 4, "end": 4, "text": "重要"}]
        plan = build_plan(source, 10)
        self.assertEqual(plan["removed"], [])
        self.assertEqual(plan["output_duration"], 10)
        self.assertTrue(any("no containing segment" in warning for warning in plan["warnings"]))

    def test_microsecond_boundary_rounding_is_clamped_without_mutating_transcript(self):
        duration = 104.471687
        source = transcript([word(0, "嗯", 104, 104.471688)], [(0, 104.471688)])
        source["segments"] = [{"start": 0, "end": 104.471688, "text": "嗯"}]
        original = copy.deepcopy(source)
        plan = build_plan(source, duration)
        self.assertEqual(source, original)
        self.assertEqual(plan["fillers"][0]["end"], duration)
        self.assertEqual(plan["keep"][-1]["source_end"], duration)
        self.assertEqual(plan["output_duration"], duration)
        self.assert_mapping(plan)

    def test_millisecond_timestamp_overrun_is_not_treated_as_rounding(self):
        for source in (
            transcript([word(0, "测试", 0, 10.001)], [(0, 10)]),
            transcript([], [(0, 10.001)]),
            {"words": [], "segments": [{"start": 0, "end": 10.001, "text": "测试"}]},
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                build_plan(source, 10)

    def test_short_edge_silences_trim_to_padding_while_internal_pauses_remain_natural(self):
        duration = 68.9867
        plan = build_plan(transcript([word(0, "对话", .584, 68.632)], [(.584, 68.632)]), duration)
        self.assertEqual(len(plan["removed"]), 2)
        self.assertAlmostEqual(plan["removed"][0]["end"], .444)
        self.assertAlmostEqual(plan["removed"][1]["start"], 68.772)
        self.assert_mapping(plan)
        short_pause = build_plan(transcript([word(0, "第一句", 0, 1), word(1, "第二句", 1.8, 3)], [(0, 1), (1.8, 3)]), 3)
        self.assertEqual(short_pause["removed"], [])

    def test_invalid_ranges_are_rejected(self):
        for start, end in ((-1, 1), (1, 0), (0, math.inf), (math.nan, 1), (0, 11)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                build_plan(transcript([word(0, "测试", start, end)], [(0, 10)]), 10)
        with self.assertRaises(ValueError):
            build_plan(transcript([], [(1, .5)]), 10)

    def test_short_source_and_empty_vad_are_retained(self):
        plan = build_plan(transcript([], []), .2)
        self.assertEqual(plan["output_duration"], .2)
        self.assertEqual(plan["removed"], [])
        self.assert_mapping(plan)


if __name__ == "__main__":
    unittest.main()
