"""Caption behavior across source cuts, uncertain alignment and ASS exports."""

from pathlib import Path
import re
import tempfile
import unittest

from talking_editor import captions
from talking_editor.planner import build_plan


def word(identifier, text, start, end):
    return {"id": identifier, "text": text, "start": start, "end": end, "probability": .99}


def uncut_plan(duration):
    return {
        "keep": [{"source_start": 0, "source_end": duration, "output_start": 0, "output_end": duration}],
        "output_duration": duration,
        "fillers": [],
    }


class CaptionMappingTests(unittest.TestCase):
    def test_zero_duration_wrap_does_not_move_opening_words_to_eof(self):
        transcript = {"words": [word(0, "这个功能", 1, 1), word(1, "很好", 1, 2)]}
        cues = captions.build_captions(transcript, uncut_plan(3),
                                       {"max_line_chars": 4, "max_lines": 1, "highlights": False})
        self.assertEqual("".join(cue["text"] for cue in cues), "这个功能很好")
        self.assertEqual(cues[0]["start"], 1)
        self.assertTrue(all(cues[i]["end"] <= cues[i + 1]["start"] for i in range(len(cues) - 1)))

    def test_top_title_uses_requested_duration_and_cannot_inject_ass_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "captions.ass"
            cues = [{"start": 1, "end": 2, "text": "字幕", "highlights": []}]
            captions.write_ass(cues, path, 1080, 1920,
                               {"title": r"标题{\\pos(0,0)}", "title_duration": 3.25})
            title_line = next(line for line in path.read_text().splitlines() if line.startswith("Dialogue: 1,"))
            self.assertIn("0:00:00.00,0:00:03.25,Title", title_line)
            self.assertNotIn(r"{\\pos", title_line)
            self.assertIn("｛", title_line)

    def test_removed_middle_maps_later_captions_to_output_time(self):
        transcript = {"words": [word(0, "第一句", 2, 3), word(1, "嗯", 5, 5.3), word(2, "第二句", 8, 9)]}
        plan = {
            "keep": [
                {"source_start": 1, "source_end": 4, "output_start": 0, "output_end": 3},
                {"source_start": 7, "source_end": 10, "output_start": 3, "output_end": 6},
            ],
            "output_duration": 6,
            "fillers": [{"word_ids": [1], "subtitle_hidden": True, "audio_removed": True}],
        }
        cues = captions.build_captions(transcript, plan, {"highlights": False})
        self.assertEqual([cue["text"] for cue in cues], ["第一句", "第二句"])
        self.assertEqual([cue["start"] for cue in cues], [1.0, 4.0])
        self.assertEqual([cue["word_ids"] for cue in cues], [[0], [2]])
        self.assertLessEqual(cues[0]["end"], cues[1]["start"])
        self.assertTrue(all(0 <= cue["start"] < cue["end"] <= 6 for cue in cues))

    def test_subtitle_only_filler_cleanup_retains_useful_and_ambiguous_words(self):
        words = [
            word(0, "我觉得", 0, .7), word(1, "嗯，", .7, .9), word(2, "那个", .9, 1.3),
            word(3, "功能", 1.3, 1.7), word(4, "就是", 1.7, 2.1), word(5, "100元", 2.1, 3),
        ]
        transcript = {"words": words, "speech_regions": [{"start": 0, "end": 3}], "segments": []}
        plan = build_plan(transcript, 3)
        self.assertEqual(plan["removed"], [])
        self.assertEqual(plan["fillers"][0]["word_ids"], [1])
        self.assertFalse(plan["fillers"][0]["audio_removed"])
        cues = captions.build_captions(transcript, plan, {"highlights": False})
        text = "".join(cue["text"] for cue in cues)
        self.assertEqual(text, "我觉得那个功能就是100元")
        self.assertEqual([identifier for cue in cues for identifier in cue["word_ids"]], [0, 2, 3, 4, 5])
        self.assertEqual(words[1]["text"], "嗯，")

    def test_zero_duration_word_stays_in_sentence_without_changing_source_times(self):
        words = [word(0, "这", 1, 1), word(1, "很好", 1, 2)]
        cues = captions.build_captions({"words": words}, uncut_plan(3), {"highlights": False})
        self.assertEqual("".join(cue["text"] for cue in cues), "这很好")
        self.assertEqual(cues[0]["word_ids"], [0, 1])
        self.assertEqual((words[0]["start"], words[0]["end"]), (1, 1))

    def test_lone_zero_duration_word_at_eof_gets_visible_display_time(self):
        words = [word(0, "好", 3, 3)]
        cues = captions.build_captions({"words": words}, uncut_plan(3), {"highlights": False})
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "好")
        self.assertEqual(cues[0]["word_ids"], [0])
        self.assertLess(cues[0]["start"], cues[0]["end"])
        self.assertLessEqual(cues[0]["end"], 3)
        self.assertEqual((words[0]["start"], words[0]["end"]), (3, 3))

    def test_zero_duration_final_word_is_merged_into_previous_cue(self):
        words = [word(0, "继续", 0, 1), word(1, "好", 3, 3)]
        cues = captions.build_captions({"words": words}, uncut_plan(3), {"highlights": False})
        self.assertEqual([identifier for cue in cues for identifier in cue["word_ids"]], [0, 1])
        self.assertEqual("".join(cue["text"] for cue in cues), "继续好")
        self.assertTrue(all(cue["end"] > cue["start"] for cue in cues))

    def test_zero_duration_leading_group_does_not_move_to_eof(self):
        words = [word(0, "这个功能", 1, 1), word(1, "很好", 1, 2)]
        cues = captions.build_captions(
            {"words": words}, uncut_plan(3),
            {"highlights": False, "max_line_chars": 4, "max_lines": 1},
        )
        self.assertEqual("".join(cue["text"] for cue in cues), "这个功能很好")
        self.assertEqual([identifier for cue in cues for identifier in cue["word_ids"]], [0, 1])
        self.assertEqual(cues[0]["start"], 1)
        for left, right in zip(cues, cues[1:]):
            self.assertLessEqual(left["end"], right["start"])

    def test_english_words_have_spaces_after_punctuation(self):
        tokens = ["Hello,", "world.", "How", "are", "you?"]
        words = [word(index, token, index * .3, (index + 1) * .3) for index, token in enumerate(tokens)]
        cues = captions.build_captions({"words": words}, uncut_plan(2), {"highlights": False})
        self.assertEqual(" ".join(cue["text"] for cue in cues), "Hello, world. How are you?")


class CaptionExportTests(unittest.TestCase):
    def test_ass_transcript_cannot_inject_override_tags_or_extra_events(self):
        payload = r"{\pos(0,0)}hello\Nworld" + "\r\nDialogue: fake"
        cue = {"id": 1, "start": 0, "end": 1, "text": payload, "word_ids": [0], "highlights": []}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "captions.ass"
            captions.write_ass([cue], target, 1920, 1080, {"max_line_chars": 500})
            exported = target.read_text(encoding="utf-8")
        events = [line for line in exported.splitlines() if line.startswith("Dialogue: ")]
        self.assertEqual(len(events), 1)
        self.assertNotIn(r"{\pos(0,0)}", events[0])
        self.assertNotIn(r"\N", events[0])
        self.assertIn("｛＼pos(0,0)｝", events[0])
        self.assertIn("hello＼Nworld", events[0])

    def test_timestamp_rounding_carries_to_next_minute_and_hour(self):
        cases = [
            (59.9996, False, "00:01:00,000"),
            (3599.9996, False, "01:00:00,000"),
            (59.996, True, "0:01:00.00"),
            (3599.996, True, "1:00:00.00"),
        ]
        for seconds, centiseconds, expected in cases:
            with self.subTest(seconds=seconds, centiseconds=centiseconds):
                self.assertEqual(captions._timestamp(seconds, centiseconds), expected)

    def test_srt_and_ass_share_rounded_export_timeline(self):
        cue = {"id": 7, "start": 59.9996, "end": 3600.5, "text": "第一句", "word_ids": [0], "highlights": []}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            captions.write_srt([cue], target / "captions.srt")
            captions.write_ass([cue], target / "captions.ass", 1920, 1080)
            srt = (target / "captions.srt").read_text(encoding="utf-8")
            ass = (target / "captions.ass").read_text(encoding="utf-8")
        self.assertIn("00:01:00,000 --> 01:00:00,500", srt)
        self.assertIn("Dialogue: 0,0:01:00.00,1:00:00.50,", ass)

    def test_highlight_offsets_match_exact_original_unicode_text(self):
        text = "使用C++剪辑，效率提高100%，效果很好。"
        spans = captions._emphasis(text, ["不存在的词", "100%", "效率", "C++"])
        self.assertTrue(spans)
        for span in spans:
            self.assertEqual(text[span["start"]:span["end"]], span["text"])
            self.assertLess(span["start"], span["end"])
        for left, right in zip(spans, spans[1:]):
            self.assertLessEqual(left["end"], right["start"])
        cue = {"text": text, "highlights": spans}
        rendered = captions._ass_text(cue, 500)
        self.assertEqual(re.sub(r"\{[^}]*\}", "", rendered), text)
        self.assertEqual(rendered.count(r"{\c&H00D7FF&}"), len(spans))

    def test_ass_escaping_does_not_shift_highlight_location(self):
        text = r"{示例}\工具节省100%时间。"
        start = text.index("100%")
        cue = {"text": text, "highlights": [{"start": start, "end": start + 4, "text": "100%"}]}
        rendered = captions._ass_text(cue, 500)
        self.assertIn(r"{\c&H00D7FF&}100%{\c&HFFFFFF&}", rendered)
        self.assertIn("｛示例｝＼工具", rendered)


if __name__ == "__main__":
    unittest.main()
