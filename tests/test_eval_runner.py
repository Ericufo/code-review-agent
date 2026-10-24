import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from evals.dataset import load_cases
from evals.mocks import ScriptedModel
from evals.runner import (ROOT, check_execution, ratio, resource_summary, run_case,
                          run_dataset, summarize)


class TestEvalRunner(unittest.TestCase):
    def setUp(self):
        self.cases = load_cases()
        self.bug = next(c for c in self.cases if c.case_id == "shared-list-sort")
        self.clean = next(c for c in self.cases if c.case_id == "guaranteed-profile")

    def run_one(self, case=None, **kwargs):
        return run_case(case or self.bug, run_id="test-run", mode="context", **kwargs)

    def test_success_is_real_graph_and_only_tool_supplied_evidence_is_retained(self):
        events = []
        result = self.run_one(events=events)
        self.assertEqual((result["status"], result["reviewed_files"]), ("COMPLETE", 1))
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["evidence"][0]["revision"], result["versions"]["head"])
        self.assertEqual(result["resources"]["model_calls"], 2)
        self.assertEqual(result["resources"]["tool_attempts"], 1)
        self.assertIsNone(result["resources"]["api_usage"])
        self.assertEqual(result["resources"]["usage_missing_requests"], 2)
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[-1]["node"], "case_end")
        self.assertTrue(all(event["run_id"] == "test-run" and event["case_hash"] == self.bug.case_hash for event in events))
        self.assertEqual(result["quality_metrics"], {"precision": None, "recall": None, "evidence_valid_rate": None})

    def test_normal_zero_findings_is_complete_but_failed_normal_is_unknown(self):
        clean = self.run_one(self.clean)
        failed = self.run_one(self.clean, scenario="invalid_output")
        self.assertEqual(clean["status"], "COMPLETE")
        self.assertEqual(clean["findings"], failed["findings"])
        self.assertEqual(failed["status"], "INCOMPLETE")
        groups = summarize([clean, failed])
        self.assertEqual(groups[0]["normal_complete_zero_findings"], 1)
        self.assertEqual(groups[1]["normal_unknown"], 1)

    def test_each_injected_failure_has_expected_calls_and_reason(self):
        for scenario, calls, tools, reason in (
            ("invalid_output", 2, 0, "invalid_output"),
            ("tool_error", 2, 1, "tool_error"),
            ("budget_exhausted", 0, 0, "budget_exhausted"),
            ("model_error", 1, 0, "model_error"),
        ):
            with self.subTest(scenario=scenario):
                result = self.run_one(scenario=scenario)
                self.assertTrue(check_execution(result))
                self.assertEqual(result["resources"]["model_calls"], calls)
                self.assertEqual(result["resources"]["tool_attempts"], tools)
                self.assertEqual((result["failures"] + result["skipped"])[0]["reason"], reason)
                self.assertEqual(result["findings"], [])

    def test_partial_retains_first_finding_and_uses_distinct_reproducible_snapshot(self):
        result = self.run_one(scenario="partial")
        self.assertTrue(check_execution(result))
        self.assertEqual((result["reviewed_files"], result["eligible_files"]), (1, 2))
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["files"][1]["status"], "failed")
        self.assertNotEqual(result["case_hash"], result["source_case_hash"])
        self.assertEqual(result["source_case_hash"], self.bug.case_hash)
        group = summarize([result])[0]
        self.assertEqual(group["file_coverage"], 0.5)
        self.assertEqual(group["case_completion_rate"], 0)
        self.assertEqual(group["completed_subset"]["cases"], 0)
        self.assertIsNone(group["completed_subset"]["recall"])

    def test_budgets_and_limits_are_recorded_without_dispatched_requests(self):
        for limits in ({"max_input_chars": 0}, {"max_model_calls": 0}):
            result = self.run_one(context_limits=limits)
            self.assertEqual(result["resources"]["model_calls"], 0)
            self.assertEqual(result["skipped"][0]["reason"], "budget_exhausted")
            self.assertFalse(check_execution(result))

    def test_usage_totals_require_observations_for_every_request(self):
        model = ScriptedModel()
        original = model.context_request
        def request(*args, **kwargs):
            response = original(*args, **kwargs)
            response.usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "raw": "DO_NOT_LOG"}
            return response
        model.context_request = request
        events = []
        result = self.run_one(model=model, events=events)
        self.assertEqual(result["resources"]["api_usage"], {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24})
        self.assertNotIn("DO_NOT_LOG", json.dumps(events))
        events = [{"node": "model_request", "api_usage": {"input_tokens": 10}},
                  {"node": "repair_request", "api_usage": None}]
        self.assertIsNone(resource_summary(events)["api_usage"])
        self.assertIsNone(ratio(0, 0))

    def test_outer_execution_failure_remains_null_status_and_denominator_is_kept(self):
        events = []
        with patch("evals.runner.review_files_graph", side_effect=RuntimeError("PRIVATE_GRAPH_BODY")):
            result = self.run_one(events=events)
        self.assertEqual(result["execution_status"], "failed")
        self.assertIsNone(result["status"])
        self.assertEqual(result["eligible_files"], 1)
        self.assertEqual(result["files"][0]["status"], "unknown")
        self.assertEqual(events[-1]["outcome"], "failed")
        self.assertNotIn("PRIVATE_GRAPH_BODY", json.dumps([result, events]))

    def test_no_source_or_exception_body_in_events_and_no_gold_in_requests(self):
        captured = []
        def request(inputs, instructions, **kwargs):
            captured.append(json.dumps(inputs))
            raise RuntimeError("PRIVATE_ERROR_BODY")
        model = SimpleNamespace(diff_review=Mock(), context_request=request)
        events = []
        result = self.run_one(model=model, events=events)
        serialized = json.dumps(events)
        for source in self.bug.fixture.head.values():
            self.assertNotIn(source, serialized)
        self.assertNotIn("PRIVATE_ERROR_BODY", json.dumps(result))
        self.assertNotIn(self.bug.expectation.rationale, captured[0])
        self.assertNotIn(self.bug.case_id, captured[0])

    def test_artifacts_separate_quality_from_reliability_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            artifact = run_dataset(self.cases, output=output)
            self.assertEqual(len(artifact["results"]), 45)
            self.assertTrue(all(item["passed"] for item in artifact["execution_checks"]))
            self.assertEqual(len(artifact["summary"]), 5)
            self.assertEqual({p.name for p in output.iterdir()},
                             {"manifest.json", "results.json", "events.jsonl", "report.md", "adjudications.json"})
            events = [json.loads(line) for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(events)
            self.assertEqual(json.loads((output / "results.json").read_text(encoding="utf-8")), artifact)
            marks = json.loads((output / "adjudications.json").read_text(encoding="utf-8"))
            self.assertEqual(len(marks), 40)
            self.assertTrue(all(mark["status"] == "pending" for mark in marks))
            self.assertIn("not a model-quality baseline", (output / "report.md").read_text(encoding="utf-8"))
            original = (output / "manifest.json").read_bytes()
            with self.assertRaises(FileExistsError):
                run_dataset(self.cases, output=output)
            self.assertEqual((output / "manifest.json").read_bytes(), original)

    def test_cli_blocks_network_and_publication_and_rejects_live_flag(self):
        script = """
import socket, sys
from unittest.mock import patch
from evals.runner import main
with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')), \\
     patch('context_review.request_context_model', side_effect=AssertionError('live model forbidden')), \\
     patch('reviewer.review_diff', side_effect=AssertionError('live diff forbidden')):
    raise SystemExit(main())
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-c", script, "--output", str(Path(directory) / "run")],
                                    cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["records"], 29)
            for args in (("--live",), ("--split", "holdout")):
                result = subprocess.run([sys.executable, "-m", "evals.runner", *args],
                                        cwd=ROOT, capture_output=True, text=True, timeout=20)
                self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
