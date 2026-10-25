"""High-value checks for provenance, real interaction data and evidence coverage."""
from copy import deepcopy
import unittest

from dyxhs_guider.core import normalize_rows, select_samples, validate_analyses


NOW = "2026-10-18T20:00:00+00:00"


def sample(item_id="1", platform="douyin", **updates):
    row = {"platform": platform, "id": item_id,
           "url": f"https://www.douyin.com/video/{item_id}" if platform == "douyin" else f"https://www.xiaohongshu.com/explore/{item_id}",
           "title": "园艺训练：普通人育苗进步", "description": "每天练习基本功，调整养花心态", "caption": "",
           "content_type": "video", "published_at": "2026-10-01T00:00:00+08:00",
           "collected_at": "2026-10-18T19:00:00+00:00", "metrics": {"likes": 1000, "comments": None, "collects": None, "shares": None, "views": None}}
    row.update(updates)
    return row


def evidence_analysis(row, source="subtitle"):
    row = deepcopy(row)
    row["content_evidence"] = [{"id": "E1", "source": source, "text": "失误后先稳住心态，再把动作练熟", "url": row["url"], "start": 0, "end": 3}]
    analysis = {"sample_id": f"{row['platform']}:{row['id']}", "platform": row["platform"], "id": row["id"],
                "evidence_level": source, "observed_evidence": [{"evidence_id": "E1"}],
                "hypotheses": [{"claim": "具体失误和练习建议可能引发初学者共鸣", "evidence_ids": ["E1"]}],
                "confidence": "medium", "content_summary": "讨论失误后的心态和练习", "transferable_idea": "从一次具体失误切入练习方法",
                "limitations": ["未采样评论；单次互动快照不能确定传播原因"]}
    return row, analysis


class NormalizeTests(unittest.TestCase):
    def test_preserves_caption_author_and_evidence_without_refreshing_dates(self):
        row, _ = evidence_analysis(sample(caption="养花心态", author_name="作者", source_type="browser_response", actual_video_watched=False,
                                          source_index_crawled_at="2026-10-18", metric_observation_type="search_index_snapshot"))
        result = normalize_rows({"platform": "douyin", "samples": [row]})[0]
        self.assertEqual(result["content_evidence"], row["content_evidence"])
        self.assertEqual(result["author_name"], "作者")
        self.assertEqual(result["caption"], "养花心态")
        self.assertIsNone(result["metrics"]["comments"])
        self.assertEqual(result["collected_at"], row["collected_at"])
        self.assertEqual(normalize_rows([result])[0]["source_type"], "browser_response")
        for field in ("actual_video_watched", "source_index_crawled_at", "metric_observation_type"):
            self.assertEqual(result[field], row[field])

    def test_does_not_fabricate_url_or_assume_video(self):
        row = sample()
        del row["url"]
        del row["content_type"]
        result = normalize_rows([row])[0]
        self.assertEqual(result["url"], "")
        self.assertEqual(result["content_type"], "unknown")

    def test_rejects_lookalike_host_and_foreign_id(self):
        for url in ("https://douyin.com.evil.test/video/1", "javascript:alert(1)", "https://evil.test/douyin.com/video/1", "https://www.douyin.com/video/2"):
            result = select_samples([sample(url=url)], total=1, now=NOW)
            self.assertEqual(result["samples"], [], url)

    def test_filters_explicit_platform_without_relabelling(self):
        self.assertEqual(normalize_rows([sample(platform="xiaohongshu")], platform="douyin"), [])

    def test_wrong_platform_item_route_is_rejected(self):
        result = select_samples([sample(platform="xiaohongshu", url="https://www.xiaohongshu.com/video/1")], total=1, now=NOW)
        self.assertEqual(result["samples"], [])


class SelectionTests(unittest.TestCase):
    def test_platform_quotas_never_silently_move(self):
        result = select_samples([sample("1"), sample("2")], total=2, now=NOW)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["stats"]["platform_counts"], {"douyin": 1, "xiaohongshu": 0})
        self.assertEqual(result["stats"]["missing_by_platform"]["xiaohongshu"], 1)

    def test_unknown_and_zero_interactions_do_not_mean_popular(self):
        rows = [sample("1", metrics={"likes": None, "views": 100000}), sample("2", metrics={"likes": 0, "comments": 0}), sample("3", metrics={"likes": None, "comments": 12})]
        result = select_samples(rows, total=1, now=NOW)
        self.assertEqual([row["id"] for row in result["samples"]], ["3"])
        self.assertIsNone(result["samples"][0]["metrics"]["likes"])
        self.assertEqual(result["stats"]["excluded_counts"]["no_nonzero_interactions"], 2)

    def test_missing_likes_cannot_pass_explicit_threshold(self):
        result = select_samples([sample(metrics={"likes": None, "comments": 100})], total=1, now=NOW, min_likes=1000)
        self.assertEqual(result["excluded"][0]["reason"], "unknown_likes_for_threshold")

    def test_platform_hotness_and_author_cap(self):
        rows = [sample(str(i), author_id="same", metrics={"likes": i * 100}) for i in range(1, 6)]
        result = select_samples(rows, total=4, quotas={"douyin": 4}, max_per_author=2, now=NOW)
        self.assertEqual([row["id"] for row in result["samples"]], ["5", "4"])
        self.assertEqual(result["stats"]["excluded_counts"]["author_limit"], 3)

    def test_author_name_limits_apply_without_author_id(self):
        rows = [sample(str(i), author_name="同一个作者") for i in range(1, 5)]
        result = select_samples(rows, total=4, quotas={"douyin": 4}, max_per_author=2, now=NOW)
        self.assertEqual(len(result["samples"]), 2)
        self.assertEqual(result["stats"]["excluded_counts"]["author_limit"], 2)

    def test_strict_type_date_and_semantic_gates(self):
        rows = [sample("1", content_type="image"), sample("2", published_at=None), sample("3", published_at="2027-01-01T00:00:00Z"),
                sample("4", published_at="2020-01-01T00:00:00Z"), sample("5", title="人生自律成长", description="坚持梦想", caption=""),
                sample("6", title="园艺记录", description="坚持记录植物变化", caption="")]
        result = select_samples(rows, total=1, now=NOW, topic_keywords=["园艺"])
        self.assertEqual([row["id"] for row in result["samples"]], ["6"])
        self.assertEqual(set(result["stats"]["excluded_counts"]), {"not_confirmed_video", "unknown_published_at", "future_published_at", "outside_window", "unrelated_topic"})

    def test_caption_is_valid_topic_evidence_but_query_is_not(self):
        yes = sample("1", title="今天的记录", description="", caption="育苗基本功练习")
        no = sample("2", title="今天的记录", description="", source_query="园艺自我成长")
        result = select_samples([yes, no], total=1, now=NOW, topic_keywords=["育苗"])
        self.assertEqual([row["id"] for row in result["samples"]], ["1"])

    def test_deduplicates_platform_items_and_flags_cross_platform_candidates(self):
        first = sample("1", caption="普通人园艺训练每天坚持打好基本功")
        second = sample("1", platform="xiaohongshu", caption=first["caption"])
        result = select_samples([first, deepcopy(first), second], total=2, now=NOW)
        self.assertEqual(result["stats"]["duplicate_count"], 1)
        self.assertEqual(len(result["samples"]), 2)
        self.assertEqual(len(result["cross_platform_duplicate_candidates"]), 1)

    def test_invalid_duplicate_cannot_poison_a_valid_source(self):
        valid = sample("1")
        invalid = sample("1", url="https://evil.test/video/1", collected_at="2026-10-18T20:00:00Z")
        result = select_samples([valid, invalid], total=1, now=NOW)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["samples"][0]["url"], valid["url"])

    def test_bad_configuration_fails_explicitly(self):
        for kwargs in ({"total": 2, "quotas": {"douyin": 1}}, {"min_likes": -1}, {"max_per_author": 0}):
            with self.assertRaises(ValueError):
                select_samples([], now=NOW, **kwargs)


class EvidenceTests(unittest.TestCase):
    def test_actual_content_evidence_can_complete(self):
        for source in ("subtitle", "visual"):
            row, analysis = evidence_analysis(sample(), source)
            result = validate_analyses([row], [analysis])
            self.assertEqual(result["status"], "complete", result)
            self.assertEqual(result["stats"]["complete_count"], 1)

    def test_metadata_stays_preliminary(self):
        row, analysis = evidence_analysis(sample(), "metadata")
        result = validate_analyses([row], [analysis])
        self.assertEqual(result["status"], "analysis_incomplete")
        self.assertEqual(result["analyses"][0]["validation_status"], "preliminary")

    def test_cannot_invent_observations_or_hypothesis_references(self):
        row, analysis = evidence_analysis(sample())
        analysis["observed_evidence"][0]["text"] = "三秒内打出逆转绝杀"
        analysis["hypotheses"][0]["evidence_ids"] = ["foreign-id"]
        result = validate_analyses([row], [analysis])
        self.assertEqual(result["status"], "analysis_incomplete")
        self.assertIn("evidence_content_mismatch:E1", result["analyses"][0]["validation_errors"])
        self.assertIn("ungrounded_hypothesis", result["analyses"][0]["validation_errors"])

    def test_foreign_video_evidence_and_missing_artifacts_are_rejected(self):
        row, analysis = evidence_analysis(sample())
        row["content_evidence"][0]["url"] = "https://www.douyin.com/video/other"
        self.assertEqual(validate_analyses([row], [analysis])["status"], "analysis_incomplete")
        row["content_evidence"][0]["url"] = row["url"]
        row["content_evidence"][0]["artifact"] = "/does-not-exist/frame.jpg"
        self.assertEqual(validate_analyses([row], [analysis])["status"], "analysis_incomplete")

    def test_coverage_duplicates_and_empty_input_cannot_complete(self):
        row, analysis = evidence_analysis(sample())
        self.assertEqual(validate_analyses([row, sample("2")], [analysis])["stats"]["missing_count"], 1)
        self.assertEqual(validate_analyses([row], [analysis, analysis])["status"], "analysis_incomplete")
        self.assertEqual(validate_analyses([], [])["status"], "analysis_incomplete")

    def test_declared_visual_cannot_upgrade_metadata(self):
        row, analysis = evidence_analysis(sample(), "metadata")
        analysis["evidence_level"] = "visual"
        result = validate_analyses([row], [analysis])
        self.assertIn("overstated_evidence_level", result["analyses"][0]["validation_errors"])
        self.assertEqual(result["analyses"][0]["evidence_level"], "metadata")

    def test_malformed_json_references_report_errors_instead_of_crashing(self):
        row, analysis = evidence_analysis(sample())
        analysis["observed_evidence"] = [{"evidence_id": ["E1"]}]
        analysis["hypotheses"][0]["evidence_ids"] = [{"id": "E1"}]
        analysis["evidence_level"] = ["visual"]
        self.assertEqual(validate_analyses([row], [analysis])["status"], "analysis_incomplete")
        analysis["sample_id"] = ["douyin:1"]
        self.assertEqual(validate_analyses([row], [analysis])["stats"]["missing_count"], 1)


if __name__ == "__main__":
    unittest.main()
