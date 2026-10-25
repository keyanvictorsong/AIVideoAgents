"""Offline CLI integration: real shapes, partial status and safe failed inputs."""
from contextlib import redirect_stderr, redirect_stdout
import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dyxhs_guider import cli
from dyxhs_guider.collectors import ROOT, read_payload


def sample(item_id, platform="douyin", source_type="public_web_search", evidence_source="metadata"):
    now = datetime.now(timezone.utc)
    domain, route = ("www.douyin.com", "video") if platform == "douyin" else ("www.xiaohongshu.com", "explore")
    url = f"https://{domain}/{route}/{item_id}"
    return {"platform": platform, "id": item_id, "url": url, "content_type": "video",
            "title": f"园艺训练记录{item_id}", "caption": f"园艺基本功练习，记录进步的第{item_id}次训练", "description": "",
            "published_at": (now - timedelta(days=1)).isoformat(), "collected_at": now.isoformat(),
            "metrics": {"likes": 20000, "comments": None, "collects": None, "shares": None, "views": None},
            "source_type": source_type, "author_name": f"作者{item_id}", "actual_video_watched": False,
            "content_evidence": [{"id": "E1", "source": evidence_source, "text": f"园艺训练的记录{item_id}", "url": url}]}


def analysis(row):
    return {"sample_id": f"{row['platform']}:{row['id']}", "platform": row["platform"], "id": row["id"],
            "evidence_level": row["content_evidence"][0]["source"], "observed_evidence": [{"evidence_id": "E1"}],
            "hypotheses": [{"claim": "具体的训练建议可能有收藏价值", "evidence_ids": ["E1"]}],
            "confidence": "low", "content_summary": "展示园艺基本功练习", "transferable_idea": "围绕一个动作记录练习过程",
            "limitations": ["没有评论采样；互动不是实时快照"]}


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, payload):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def invoke(self, *args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli.main([*map(str, args), "--output", str(self.output)])

    def data(self):
        return json.loads((self.output / "report_data.json").read_text(encoding="utf-8"))

    def snapshot(self):
        return {str(path.relative_to(self.output)): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}

    def make_partial(self):
        self.assertEqual(self.invoke("prepare"), 2)
        return self.snapshot()

    def test_empty_prepare_is_partial_and_has_zero_counts(self):
        self.assertEqual(self.invoke("prepare"), 2)
        result = self.data()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["samples"], [])
        self.assertEqual(result["analysis_status"]["status"], "analysis_incomplete")
        self.assertEqual(result["collection_status"]["douyin"]["candidate_count"], 0)
        self.assertTrue((self.output / "report.html").is_file())

    def test_metadata_only_stays_partial_despite_full_sample_quotas(self):
        rows = [sample("1"), sample("2", "xiaohongshu")]
        references = self.write("rows.json", {"samples": rows})
        reviewed = self.write("reviewed.json", {"analyses": [analysis(row) for row in rows]})
        self.assertEqual(self.invoke("prepare", "--import", references, "--analyses", reviewed, "--total", "2"), 2)
        result = self.data()
        self.assertEqual(result["selection_status"], "complete")
        self.assertEqual(result["analysis_status"]["stats"]["preliminary_count"], 2)
        self.assertEqual(result["status"], "partial")

    def test_content_review_completes_and_survives_render_without_original_file(self):
        rows = [sample("1", evidence_source="subtitle"), sample("2", "xiaohongshu", evidence_source="visual")]
        references = self.write("rows.json", {"samples": rows})
        reviewed = self.write("reviewed.json", [analysis(row) for row in rows])
        self.assertEqual(self.invoke("prepare", "--import", references, "--analyses", reviewed, "--total", "2"), 0)
        reviewed.unlink()
        self.assertEqual(self.invoke("render"), 0)
        self.assertEqual(self.data()["analysis_status"]["stats"]["complete_count"], 2)

    def test_cross_platform_duplicate_candidates_cannot_complete(self):
        rows = [sample("1", evidence_source="subtitle"), sample("2", "xiaohongshu", evidence_source="visual")]
        rows[1]["caption"] = rows[0]["caption"]
        references = self.write("rows.json", rows)
        reviewed = self.write("reviewed.json", [analysis(row) for row in rows])
        self.assertEqual(self.invoke("prepare", "--import", references, "--analyses", reviewed, "--total", "2"), 2)
        self.assertEqual(self.data()["status"], "partial")

    def test_missing_import_preserves_every_existing_artifact(self):
        before = self.make_partial()
        self.assertEqual(self.invoke("prepare", "--import", self.root / "missing.json"), 1)
        self.assertEqual(self.snapshot(), before)

    def test_corrupt_import_and_analysis_preserve_every_existing_artifact(self):
        before = self.make_partial()
        broken = self.root / "broken.json"
        broken.write_text("{", encoding="utf-8")
        good = self.write("good.json", [sample("1")])
        for arguments in (("--import", broken), ("--import", good, "--analyses", broken),
                          ("--import", good, "--analyses", self.root / "missing.json")):
            self.assertEqual(self.invoke("prepare", *arguments), 1)
            self.assertEqual(self.snapshot(), before)

    def test_run_preflights_inputs_and_quotas_before_collecting(self):
        before = self.make_partial()
        with patch.object(cli, "collect_platform") as collector:
            self.assertEqual(self.invoke("run", "--import", self.root / "missing.json"), 1)
            self.assertEqual(self.invoke("run", "--total", "2", "--douyin-quota", "3"), 1)
            collector.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_render_failure_does_not_partially_replace_report_data(self):
        before = self.make_partial()
        good = self.write("good.json", [sample("1")])
        with patch.object(cli, "render_report", side_effect=RuntimeError("render failed")):
            self.assertEqual(self.invoke("prepare", "--import", good), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.output.glob(".guider-stage-*")))

    def test_platform_failure_status_is_retained_with_index_supplement_counts(self):
        self.output.mkdir()
        self.write("output/collection_status.json", {"douyin": {"status": "rate_limited", "reason": "验证码"}})
        references = self.write("rows.json", {"platform": "douyin", "status": "ok", "samples": [sample("1")]})
        self.assertEqual(self.invoke("prepare", "--import", references, "--total", "2"), 2)
        entry = self.data()["collection_status"]["douyin"]
        self.assertEqual(entry["status"], "rate_limited")
        self.assertEqual(entry["reason"], "验证码")
        self.assertEqual(entry["candidate_count"], 1)
        self.assertEqual(entry["selected_count"], 1)
        self.assertEqual(entry["platform_candidate_count"], 0)
        self.assertEqual(entry["public_index_candidate_count"], 1)
        self.assertIn("非实时指标", entry["source"])

    def test_duplicate_import_paths_do_not_change_original_provenance(self):
        references = self.write("rows.json", [sample("1")])
        self.assertEqual(self.invoke("prepare", "--import", references, "--import", references, "--total", "1"), 2)
        result = self.data()
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["samples"][0]["source_type"], "public_web_search")
        self.assertFalse(result["samples"][0]["actual_video_watched"])
        self.assertEqual(result["samples"][0]["source_file"], str(references.resolve()))

    def test_render_count_mismatch_cannot_trust_old_selection_complete(self):
        row = sample("1", evidence_source="subtitle")
        references = self.write("rows.json", [row])
        reviewed = self.write("reviewed.json", [analysis(row)])
        self.assertEqual(self.invoke("prepare", "--import", references, "--analyses", reviewed, "--total", "1"), 0)
        payload = self.data()
        payload["target_total"] = 50
        (self.output / "report_data.json").write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.invoke("render"), 2)
        self.assertEqual(self.data()["status"], "partial")

    def test_csv_preserves_evidence_metrics_and_missing_source_url(self):
        rows = [sample("1")]
        rows[0]["url"] = ""
        path = self.root / "rows.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in rows[0].items()})
        payload = read_payload(path)
        self.assertEqual(payload["samples"][0]["content_evidence"], rows[0]["content_evidence"])
        self.assertIsNone(payload["samples"][0]["metrics"]["comments"])
        self.assertEqual(self.invoke("prepare", "--import", path, "--total", "1"), 2)
        self.assertEqual(self.data()["samples"], [])

    def test_launcher_works_from_an_unrelated_directory(self):
        result = subprocess.run([str(ROOT / "guider"), "doctor"], cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["title_tag_source_exists"])


if __name__ == "__main__":
    unittest.main()
