import copy
import json
from pathlib import Path
import tempfile
import unittest

from evals.dataset import load_cases
from evals.runner import adjudication_template, run_case
from evals.score import main, score


class TestEvalScore(unittest.TestCase):
    def setUp(self):
        self.cases = [c for c in load_cases() if c.case_id in ("shared-list-sort", "guaranteed-profile")]
        rows = [run_case(c, run_id="run-1", mode=mode) for c in self.cases for mode in ("diff-only", "context")]
        self.artifact = {"manifest": {"run_id": "run-1", "rubric_version": 1, "execution_kind": "mock",
                          "cases": [{"case_id": c.case_id, "case_hash": c.case_hash, "split": c.split} for c in self.cases]},
                         "results": rows}
        self.marks = self.completed(rows)

    def completed(self, rows):
        marks = adjudication_template(rows)
        for mark in marks:
            mark.update(status="completed", reviewer="Test reviewer", date="2026-10-23")
            for finding in mark["findings"]:
                finding.update(verdict="TP", defect_id="cache-order-mutation", reason="Test: confirmed mutation of CACHE",
                               evidence_valid=True if "/context/" in mark["record_id"] else None)
        return marks

    def evaluate(self, artifact=None, marks=None):
        return score(artifact or self.artifact, marks or self.marks, allow_mock=True)

    def test_mock_is_explicit_and_all_cases_have_separate_completed_denominators(self):
        with self.assertRaisesRegex(ValueError, "Mock scoring"):
            score(self.artifact, self.marks)
        result = self.evaluate()
        self.assertEqual(result["status"], "adjudicated")
        self.assertEqual(result["interpretation"], "mock plumbing exercise")
        for group in result["groups"]:
            m = group["all_cases"]
            self.assertEqual((m["cases"], m["gold"], m["TP"], m["FP"], m["TN"]), (2, 1, 1, 0, 1))
            self.assertEqual((m["precision"], m["recall"], m["normal_false_positive_rate"]), (1, 1, 0))
            self.assertEqual(group["completed_subset"], m)
            self.assertEqual(m["evidence_valid_rate"], 1 if group["mode"] == "context" else None)

    def test_unfinished_files_are_unobserved_not_fn_or_tn(self):
        art = copy.deepcopy(self.artifact)
        for row in art["results"]:
            row.update(status="INCOMPLETE", reviewed_files=0, findings=[])
            row["files"][0]["status"] = "failed"
        result = self.evaluate(art, self.completed(art["results"]))
        for group in result["groups"]:
            m = group["all_cases"]
            self.assertEqual((m["FN"], m["unobserved"], m["TN"], m["unknown"]), (0, 1, 0, 1))
            self.assertEqual(m["recall"], 0)
            self.assertIsNone(m["precision"])
            self.assertEqual(group["completed_subset"]["cases"], 0)
            self.assertIsNone(group["completed_subset"]["recall"])

    def test_complete_zero_findings_for_gold_is_fn(self):
        art = copy.deepcopy(self.artifact)
        for row in art["results"]:
            row["findings"] = []
        result = self.evaluate(art, self.completed(art["results"]))
        for group in result["groups"]:
            self.assertEqual((group["all_cases"]["FN"], group["all_cases"]["unobserved"]), (1, 0))

    def test_pending_judgments_suppress_final_rates_across_all_groups(self):
        marks = copy.deepcopy(self.marks)
        marks[0]["status"] = "pending"
        result = self.evaluate(marks=marks)
        self.assertEqual(result["status"], "pending")
        for group in result["groups"]:
            for subset in ("all_cases", "completed_subset"):
                self.assertIsNone(group[subset]["precision"])
                self.assertIsNone(group[subset]["recall"])

    def test_rejects_stale_hash_missing_rows_and_invented_findings(self):
        for field, value in (("case_hash", "old-hash"), ("run_id", "another-run"),
                             ("reviewer", ""), ("date", "not-a-date")):
            marks = copy.deepcopy(self.marks)
            marks[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.evaluate(marks=marks)
        with self.assertRaises(ValueError):
            self.evaluate(marks=self.marks[:-1])
        with self.assertRaises(ValueError):
            self.evaluate(marks=[*self.marks, self.marks[0]])
        art = copy.deepcopy(self.artifact)
        art["results"].pop()
        with self.assertRaises(ValueError):
            self.evaluate(art)

    def test_duplicate_counts_once_and_cycles_and_double_tp_are_rejected(self):
        art, marks = copy.deepcopy(self.artifact), copy.deepcopy(self.marks)
        index = next(i for i, r in enumerate(art["results"]) if r["findings"])
        art["results"][index]["findings"].append({**art["results"][index]["findings"][0], "finding_id": "f2"})
        marks[index]["findings"].append({**marks[index]["findings"][0], "finding_id": "f2", "duplicate_of": "f1"})
        result = self.evaluate(art, marks)
        record = next(r for r in result["records"] if r["record_id"] == marks[index]["record_id"])
        self.assertEqual((record["TP"], record["raw_findings"], record["unique_findings"]), (1, 2, 1))
        marks[index]["findings"][1]["duplicate_of"] = None
        with self.assertRaises(ValueError):
            self.evaluate(art, marks)
        marks[index]["findings"][1]["duplicate_of"] = "f1"
        marks[index]["findings"][0]["duplicate_of"] = "f2"
        with self.assertRaises(ValueError):
            self.evaluate(art, marks)

    def test_split_claims_preserve_original_finding_id_without_duplicate_credit(self):
        marks = copy.deepcopy(self.marks)
        index = next(i for i, m in enumerate(marks) if m["findings"] and "/diff-only/" in m["record_id"])
        first = marks[index]["findings"][0]
        marks[index]["findings"] = [{**first, "finding_id": "f1.c1", "source_finding_id": "f1"},
                                    {**first, "finding_id": "f1.c2", "source_finding_id": "f1",
                                     "defect_id": None, "verdict": "FP", "reason": "Unsupported second claim"}]
        group = next(g for g in self.evaluate(marks=marks)["groups"] if g["mode"] == "diff-only")
        self.assertEqual(group["all_cases"]["precision"], 0.5)
        self.assertEqual(group["all_cases"]["raw_findings"], 1)
        self.assertEqual(group["all_cases"]["unique_findings"], 2)

    def test_citations_and_missing_evidence_are_distinct_from_correctness(self):
        marks = copy.deepcopy(self.marks)
        index = next(i for i, m in enumerate(marks) if m["findings"] and "/context/" in m["record_id"])
        marks[index]["findings"][0]["evidence_valid"] = False
        group = next(g for g in self.evaluate(marks=marks)["groups"] if g["mode"] == "context")
        self.assertEqual(group["all_cases"]["precision"], 1)
        self.assertEqual(group["all_cases"]["evidence_valid_rate"], 0)
        marks[index]["findings"][0]["evidence_valid"] = None
        with self.assertRaises(ValueError):
            self.evaluate(marks=marks)
        marks[index]["findings"][0]["evidence_valid"] = True
        art = copy.deepcopy(self.artifact)
        art["results"][index]["findings"][0]["evidence"] = []
        with self.assertRaises(ValueError):
            self.evaluate(art, marks)

    def test_contradictory_execution_and_gold_metadata_are_rejected(self):
        for field, value in (("reviewed_files", 0), ("eligible_files", 0), ("expected_defects", 5), ("split", "holdout")):
            art = copy.deepcopy(self.artifact)
            art["results"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.evaluate(art)

    def test_cli_writes_separate_scores_and_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "results.json"
            raw.write_text(json.dumps(self.artifact), encoding="utf-8")
            marks = root / "adjudications.manual.json"
            marks.write_text(json.dumps(self.marks), encoding="utf-8")
            original = raw.read_bytes()
            args = ["--run", str(root), "--adjudications", str(marks), "--output", str(root / "scores"), "--allow-mock"]
            self.assertEqual(main(args), 0)
            with self.assertRaises(SystemExit):
                main(args)
            self.assertEqual(raw.read_bytes(), original)
            self.assertIn("mock plumbing", (root / "scores/report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
