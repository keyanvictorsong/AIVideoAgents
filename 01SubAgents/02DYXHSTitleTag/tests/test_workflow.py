import copy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dyxhs_title_tag.generation import validate_copy, validate_facts, evidence_hash
from dyxhs_title_tag.llm import ModelError, parse_json_object
from dyxhs_title_tag.pipeline import add_user_description, assemble_output, export_output, rank_collections
from dyxhs_title_tag.ranking import analyze_platform_samples, deduplicate_samples, parse_count, rank_samples, select_tags
from dyxhs_title_tag.sources import import_references, normalize_reference, parse_published_at, _page_block
from dyxhs_title_tag.video import merge_ocr_segments, parse_subtitles


NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def sample(index=1, platform="xiaohongshu", **changes):
    row = {"platform": platform, "id": str(index),
           "url": f"https://www.xiaohongshu.com/explore/{index}" if platform == "xiaohongshu" else f"https://www.douyin.com/video/{index}",
           "title": "园艺与团队配合", "description": "#园艺 #团队配合 #减肥 #养花心态", "tags": [],
           "published_at": (NOW - timedelta(days=1)).isoformat(), "collected_at": NOW.isoformat(),
           "metrics": {"likes": 100, "comments": None, "favorites": 50, "shares": None},
           "content_type": "video", "author_id": f"author-{index}"}
    row.update(changes)
    return row


def facts():
    return {"source_sha256": "abc", "summary": "作者谈园艺团队配合。", "keywords": ["园艺", "团队配合"],
            "facts": [{"id": "F1", "text": "作者讲园艺团队配合。", "evidence_ids": ["ocr-1"]}]}


def draft():
    return {"source_sha256": "abc", "generation_mode": "assistant_reviewed",
            "xiaohongshu": {"variants": [{"title": "养花先学会配合", "description": "我在园艺里学习认识自己。", "fact_ids": ["F1"]}],
                            "tag_suggestions": ["园艺", "团队配合", "养花心态"]},
            "douyin": {"variants": [{"title": "园艺里的团队配合", "description": "一起聊聊养花和团队配合。", "fact_ids": ["F1"]}]}}


class RankingTests(unittest.TestCase):
    def test_visible_count_units_and_missing(self):
        self.assertEqual(parse_count("1.2万"), 12000)
        self.assertEqual(parse_count("2.5k"), 2500)
        self.assertIsNone(parse_count(None))
        self.assertIsNone(parse_count(float("nan")))
        self.assertIsNone(parse_count(-1))

    def test_undated_future_old_and_image_do_not_count(self):
        rows = [sample(1, published_at=None), sample(2, published_at=(NOW + timedelta(days=1)).isoformat()),
                sample(3, published_at=(NOW - timedelta(days=40)).isoformat()), sample(4, content_type="image"), sample(5)]
        result = rank_samples(rows, ["园艺"], now=NOW)
        self.assertEqual([s["id"] for s in result["ranked_samples"]], ["5"])
        self.assertEqual(result["status"], "insufficient_samples")

    def test_missing_metrics_stay_unknown_but_favorites_are_preserved(self):
        row = deduplicate_samples([sample()])[0]
        self.assertEqual(row["metrics"]["collects"], 50)
        self.assertIsNone(row["metrics"]["comments"])

    def test_no_metrics_cannot_count_as_popular_reference(self):
        result = rank_samples([sample(metrics={})], ["园艺"], now=NOW)
        self.assertEqual(result["ranked_samples"], [])
        self.assertEqual(result["stats"]["platforms"]["xiaohongshu"]["excluded"]["missing_engagement_metrics"], 1)

    def test_duplicates_merge_id_url_transitively(self):
        a = sample(1)
        b = sample(1, url="https://www.xiaohongshu.com/explore/second")
        c = sample(2, url=b["url"])
        self.assertEqual(len(deduplicate_samples([a, b, c])), 1)
        self.assertEqual(len(deduplicate_samples([a, sample(1, platform="douyin")])), 2)

    def test_platforms_rank_independently(self):
        result = rank_samples([sample(), sample(2, "douyin", metrics={"likes": 1000000})], ["园艺"], now=NOW, min_references=1)
        self.assertEqual([r["ranking"]["platform_rank"] for r in result["ranked_samples"]], [1, 1])

    def test_30_day_expansion_is_explicit(self):
        result = analyze_platform_samples([sample(published_at=(NOW-timedelta(days=12)).isoformat())], ["园艺"], now=NOW, min_references=1)
        self.assertEqual(result["platforms"]["xiaohongshu"]["window_days"], 30)

    def test_unrelated_tag_is_rejected_and_count_is_not_filled(self):
        ranked = rank_samples([sample()], ["园艺", "团队配合", "心态"], now=NOW)["ranked_samples"]
        result = select_tags(ranked, ["园艺", "团队配合", "心态"])
        self.assertEqual(result["status"], "insufficient_tag_evidence")
        self.assertNotIn("减肥", [r["tag"] for r in result["tags"]])
        self.assertEqual(len(result["tags"]), 3)
        self.assertTrue(all(r["urls"] for r in result["tags"]))

    def test_basketball_does_not_make_weight_loss_tag_relevant(self):
        result = select_tags([sample(tags=["运动减脂"])], ["园艺"])
        self.assertNotIn("运动减脂", [r["tag"] for r in result["tags"]])

    def test_one_author_does_not_multiply_tag_heat(self):
        one = select_tags([sample(1, author_id="same")], ["园艺"])["tags"][0]
        many = select_tags([sample(i, author_id="same") for i in range(1, 6)], ["园艺"])["tags"][0]
        self.assertEqual(one["score"], many["score"])
        self.assertEqual(many["author_count"], 1)


class SourceTests(unittest.TestCase):
    def test_import_relative_date_does_not_get_fresh_collection_time(self):
        row = sample(published_at="1天前")
        row.pop("collected_at")
        reference = normalize_reference(row)
        self.assertIsNone(reference.collected_at)
        self.assertIsNone(reference.published_at)

    def test_relative_date_uses_actual_observation(self):
        self.assertEqual(parse_published_at("1天前", observed_at=NOW.isoformat()), (NOW-timedelta(days=1)).isoformat())

    def test_import_and_export_preserve_platform_and_collects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "references.json"
            path.write_text(json.dumps({"samples": [sample(), sample(2, "douyin")]}))
            rows = import_references(path)
            self.assertEqual({r.platform for r in rows}, {"douyin", "xiaohongshu"})
            self.assertEqual(rows[0].metrics["collects"], 50)

    def test_captcha_and_login_are_reported(self):
        self.assertEqual(_page_block("请先登录后搜索", "https://example.com")[0], "login_required")
        self.assertEqual(_page_block("请完成安全验证", "https://example.com")[0], "rate_limited")

    def test_xhs_topics_and_sensitive_query_removed(self):
        row = sample(url="https://www.xiaohongshu.com/explore/1?xsec_token=private", description="#园艺[话题]#")
        normalized = normalize_reference(row)
        self.assertEqual(normalized.tags, ["园艺"])
        self.assertNotIn("private", normalized.url)


class VideoEvidenceTests(unittest.TestCase):
    def test_srt_and_webvtt_keep_timing(self):
        for text in ("1\n00:00:01,000 --> 00:00:03,500\n园艺\n", "WEBVTT\n\n00:01.000 --> 00:03.500\n<b>园艺</b>\n"):
            cue = parse_subtitles(text)[0]
            self.assertEqual((cue["start"], cue["end"], cue["text"]), (1, 3.5, "园艺"))

    def test_ocr_blank_frame_breaks_repeated_segment(self):
        frames = [{"id": f"f{i}", "timestamp_seconds": i*2, "path": f"/tmp/frame{i}.jpg"} for i in range(4)]
        records = [{"path": f["path"], "engine": "test", "lines": [] if i == 2 else [{"text": "园艺", "confidence": 1}]} for i, f in enumerate(frames)]
        result = merge_ocr_segments(frames, records, 7, 2)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["frame_ids"], ["f0", "f1"])
        self.assertEqual(result[-1]["end"], 7)

    def test_user_description_is_labelled_not_observed(self):
        analysis = add_user_description({"metadata": {"duration": 10}, "evidence": []}, "三个人在室外育苗")
        self.assertEqual(analysis["evidence"][0]["source"], "user_description")
        self.assertEqual(analysis["evidence"][0]["timing"], "user_supplied_not_observed")


class ExportTests(unittest.TestCase):
    def test_wrong_video_fingerprint_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_copy(draft(), {**facts(), "source_sha256": "different"})

    def test_invented_evidence_is_rejected(self):
        analysis = {"source": {"sha256": "abc"}, "evidence": [], "frames": []}
        value = {**facts(), "evidence_sha256": evidence_hash(analysis)}
        with self.assertRaises(ValueError):
            validate_facts(value, analysis)

    def test_changed_description_invalidates_old_facts(self):
        analysis = {"source": {"sha256": "abc"}, "metadata": {"duration": 10},
                    "evidence": [{"id": "ocr-1", "text": "园艺团队配合"}], "frames": []}
        value = {**facts(), "evidence_sha256": evidence_hash(analysis)}
        validate_facts(value, analysis)
        changed = add_user_description(copy.deepcopy(analysis), "这次补充了完全不同的内容")
        with self.assertRaises(ValueError):
            validate_facts(value, changed)

    def test_unsupported_game_result_is_rejected(self):
        value = draft()
        value["douyin"]["variants"][0]["title"] = "今天完成绝杀"
        with self.assertRaises(ValueError):
            validate_copy(value, facts())

    def test_empty_platform_remains_incomplete_and_tags_unverified(self):
        collections = {p: {"samples": [], "status": "login_required", "reason": "需要登录"} for p in ("xiaohongshu", "douyin")}
        ranking = rank_collections(collections, ["园艺"])
        output = assemble_output(draft(), facts(), collections, ranking)
        self.assertEqual(output["status"], "copy_ready_references_incomplete")
        self.assertTrue(all(t["evidence_status"] == "related_unverified" for t in output["xiaohongshu"]["tag_details"]))
        with tempfile.TemporaryDirectory() as directory:
            export_output(output, Path(directory))
            text = (Path(directory) / "output.md").read_text()
            self.assertIn("近期热度未验证", text)
            self.assertIn("0/50", text)
            self.assertTrue((Path(directory) / "douyin-copy.json").is_file())

    def test_fifty_dated_fixture_rows_per_platform_can_complete(self):
        tag_names = [f"园艺话题{i}" for i in range(10)]
        collections = {p: {"samples": [sample(i, p, tags=tag_names) for i in range(50)], "status": "ok", "reason": "test fixtures"}
                       for p in ("xiaohongshu", "douyin")}
        # Time is frozen for fixture filtering, not copied into production data.
        with patch("dyxhs_title_tag.pipeline.analyze_platform_samples", side_effect=lambda rows, keywords, **kw: analyze_platform_samples(rows, keywords, now=NOW, **kw)):
            ranking = rank_collections(collections, ["园艺"])
        output = assemble_output(draft(), facts(), collections, ranking)
        self.assertEqual(output["status"], "complete")
        self.assertEqual(len(output["xiaohongshu"]["tags"]), 10)

    def test_model_json_must_be_structured(self):
        self.assertEqual(parse_json_object('```json\n{"x":1}\n```'), {"x": 1})
        with self.assertRaises(ModelError):
            parse_json_object("not JSON")
        with self.assertRaises(ModelError):
            parse_json_object("[]")


class TopicHandoffTests(unittest.TestCase):
    def output(self, reviewed=None, video_facts=None):
        video_facts = facts() if video_facts is None else video_facts
        collections = {p: {"samples": [], "status": "login_required", "reason": "需要登录"}
                       for p in ("xiaohongshu", "douyin")}
        ranking = rank_collections(collections, video_facts.get("keywords", []))
        return assemble_output(draft() if reviewed is None else reviewed, video_facts, collections, ranking)

    def test_default_handoff_enables_current_keywords_and_exports_to_publisher_copy(self):
        result = self.output()
        settings = result["douyin"]["topic_selection"]
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["queries"], ["园艺", "团队配合"])
        self.assertEqual(settings["keywords"], settings["queries"])
        self.assertEqual(settings["target_count"], 5)
        with tempfile.TemporaryDirectory() as directory:
            export_output(result, Path(directory))
            exported = json.loads((Path(directory) / "douyin-copy.json").read_text())
            full = json.loads((Path(directory) / "output.json").read_text())
        self.assertEqual(exported["topic_selection"], settings)
        self.assertEqual(full["douyin"]["topic_selection"], settings)
        self.assertEqual(exported["source_sha256"], facts()["source_sha256"])
        self.assertEqual(exported["copy_source"], "assistant_reviewed")
        self.assertEqual(exported["hashtags"], [])

    def test_reusing_draft_with_new_video_facts_does_not_inherit_previous_topic(self):
        reviewed = draft()
        work = {**facts(), "keywords": ["职场", "情绪管理"]}
        first = self.output(reviewed, work)
        second = self.output(reviewed, facts())
        self.assertEqual(first["douyin"]["topic_selection"]["queries"], ["职场", "情绪管理"])
        self.assertEqual(second["douyin"]["topic_selection"]["queries"], ["园艺", "团队配合"])
        self.assertNotIn("topic_selection", reviewed["douyin"])

    def test_explicit_reviewed_constraints_override_defaults(self):
        reviewed = draft()
        reviewed["douyin"]["topic_selection"] = {
            "enabled": True, "queries": [" #团队配合 ", "团队配合"], "keywords": ["配合"],
            "target_count": 3, "allowed_names": ["团队配合"], "exclude_keywords": ["营销"],
        }
        settings = self.output(reviewed)["douyin"]["topic_selection"]
        self.assertEqual(settings["queries"], ["团队配合"])
        self.assertEqual(settings["keywords"], ["配合"])
        self.assertEqual(settings["target_count"], 3)
        self.assertEqual(settings["allowed_names"], ["团队配合"])
        self.assertEqual(settings["exclude_keywords"], ["营销"])

    def test_explicit_disable_survives_export(self):
        reviewed = draft()
        reviewed["douyin"]["topic_selection"] = {"enabled": False}
        result = self.output(reviewed)
        with tempfile.TemporaryDirectory() as directory:
            export_output(result, Path(directory))
            exported = json.loads((Path(directory) / "douyin-copy.json").read_text())
        self.assertFalse(exported["topic_selection"]["enabled"])
        self.assertEqual(exported["topic_selection"]["disabled_reason"], "explicitly_disabled")
        self.assertEqual(exported["topic_selection"]["queries"], [])

    def test_legacy_missing_or_empty_fact_keywords_disable_without_inventing_seeds(self):
        for keywords in (None, []):
            video_facts = facts()
            video_facts.pop("keywords")
            if keywords is not None:
                video_facts["keywords"] = keywords
            with self.subTest(keywords=keywords):
                result = self.output(video_facts=video_facts)
                settings = result["douyin"]["topic_selection"]
                self.assertFalse(settings["enabled"])
                self.assertEqual(settings["queries"], [])
                self.assertEqual(settings["disabled_reason"], "missing_fact_keywords")

    def test_invalid_constraints_fail_before_export(self):
        for settings in ({"enabled": "false"}, {"enabled": True, "queries": []},
                         {"target_count": True}, {"target_count": 0}, {"keywords": "园艺"},
                         {"queries": [42]}, None):
            with self.subTest(settings=settings):
                reviewed = draft()
                reviewed["douyin"]["topic_selection"] = settings
                with self.assertRaises(ValueError):
                    self.output(reviewed)

    def test_handoff_does_not_turn_search_terms_into_heat_or_selected_evidence(self):
        result = self.output()
        self.assertEqual(result["status"], "copy_ready_references_incomplete")
        self.assertEqual(result["provenance"]["reference_counts"], {"xiaohongshu": 0, "douyin": 0})
        self.assertEqual(result["provenance"]["tag_evidence"], "insufficient_tag_evidence")
        self.assertNotIn("topic_selection_result", result["douyin"])
        reviewed = draft()
        reviewed["douyin"]["topic_selection"] = {"queries": ["园艺"], "verified_hot": True}
        with self.assertRaisesRegex(ValueError, "不认证热度"):
            self.output(reviewed)


if __name__ == "__main__":
    unittest.main()
