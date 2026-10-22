import unittest
from unittest.mock import Mock, patch

from diff_parser import FileDiff
from review_models import ReviewIssue, ReviewResult
from review_pipeline import review_files


class TestReviewGraph(unittest.TestCase):
    def test_engines_agree_on_results_budgets_and_model_inputs(self):
        files = [
            FileDiff("one.py", "first"),
            FileDiff("missing.py", "", skip_reason="patch unavailable"),
            FileDiff("binary.png", "Binary files a/x and b/x differ\n"),
            FileDiff("large.py", "x" * 51),
            FileDiff("broken.py", "failure"),
            FileDiff("two.py", "second"),
            FileDiff("last.py", "last"),
        ]
        for limits in (
            {}, {"max_files": 2}, {"max_total_chars": 13},
            {"max_files": 0}, {"max_total_chars": 0},
        ):
            results, calls = [], []
            for engine in ("classic", "langgraph"):
                with self.subTest(engine=engine, limits=limits):
                    def model(diff):
                        if diff == "failure":
                            raise TimeoutError("simulated timeout")
                        return ReviewResult(summary="ignored", issues=[ReviewIssue(
                            severity="high" if diff == "second" else "low",
                            file="model-invented.py", title=diff, description="A defect",
                        )])

                    with patch("review_pipeline.review_diff", side_effect=model) as mock:
                        results.append(review_files(
                            files, engine=engine, max_file_chars=50, **limits,
                        ))
                        calls.append(mock.call_args_list)
            self.assertEqual(results[0], results[1])
            self.assertEqual(calls[0], calls[1])

    @patch("review_pipeline.review_diff")
    def test_empty_input_finishes_without_model(self, model):
        steps = []
        batch = review_files([], engine="langgraph", on_step=steps.append)
        model.assert_not_called()
        self.assertFalse(batch.incomplete)
        self.assertEqual(batch.total_files, 0)
        self.assertEqual([s.node for s in steps], ["check_file", "summarize"])

    @patch("review_pipeline.review_diff")
    def test_trace_shows_failure_then_continuation_without_source_text(self, model):
        model.side_effect = [TimeoutError("timeout"), ReviewResult(summary="", issues=[])]
        steps = []
        batch = review_files([
            FileDiff("bad.py", "secret diff"),
            FileDiff("missing.py", "", skip_reason="unavailable"),
            FileDiff("good.py", "ok"),
        ], engine="langgraph", on_step=steps.append)
        self.assertEqual([s.node for s in steps], [
            "check_file", "review_file", "check_file", "skip_file",
            "check_file", "review_file", "check_file", "summarize",
        ])
        self.assertEqual([s.path for s in steps[1:6:2]], ["bad.py", "missing.py", "good.py"])
        final = steps[-1]
        self.assertEqual((final.attempted, final.reviewed, final.failed, final.skipped), (2, 1, 1, 1))
        self.assertEqual(final.used_chars, 13)
        self.assertIsNone(final.path)
        self.assertNotIn("secret diff", repr(steps))
        self.assertTrue(batch.incomplete)

    @patch("review_pipeline.review_diff", return_value=ReviewResult(summary="", issues=[]))
    def test_many_files_finish_and_keep_budget_exhausted_files_in_coverage(self, model):
        batch = review_files(
            [FileDiff(f"{i}.py", "diff") for i in range(100)], engine="langgraph",
        )
        self.assertEqual(model.call_count, 30)
        self.assertEqual((batch.total_files, batch.reviewed_files, len(batch.skipped)), (100, 30, 70))

    @patch("review_pipeline.review_diff")
    def test_all_skipped_files_finish_even_with_zero_model_budget(self, model):
        batch = review_files([
            FileDiff(f"{i}.py", "", skip_reason="unavailable") for i in range(100)
        ], engine="langgraph", max_files=0)
        model.assert_not_called()
        self.assertEqual(len(batch.skipped), 100)
        self.assertIn("No conclusion", batch.result.summary)

    @patch("review_pipeline.review_diff")
    def test_invalid_result_is_a_failure_and_spends_budget(self, model):
        invalid = ReviewIssue.model_construct(
            severity="unknown", file="wrong.py", title="bad", description="invalid",
        )
        model.return_value = Mock(issues=[invalid])
        batch = review_files([
            FileDiff("invalid.py", "diff"), FileDiff("later.py", "diff"),
        ], engine="langgraph", max_files=1)
        model.assert_called_once()
        self.assertEqual(batch.reviewed_files, 0)
        self.assertEqual(batch.failures[0].path, "invalid.py")
        self.assertIn("ValidationError", batch.failures[0].reason)
        self.assertEqual(batch.skipped[0].path, "later.py")

    @patch("review_pipeline.review_diff", return_value=ReviewResult(summary="", issues=[]))
    def test_successive_runs_do_not_share_budget_or_findings(self, model):
        for _ in range(2):
            batch = review_files([FileDiff("ok.py", "diff")], engine="langgraph", max_files=1)
            self.assertEqual(batch.reviewed_files, 1)
            self.assertFalse(batch.incomplete)
        self.assertEqual(model.call_count, 2)

    @patch("review_pipeline.review_diff")
    def test_invalid_options_fail_before_calling_model(self, model):
        for kwargs in (
            {"engine": "unknown"}, {"engine": "classic", "on_step": lambda _: None},
            {"engine": "langgraph", "max_files": -1},
            {"engine": "langgraph", "max_file_chars": -1},
            {"engine": "langgraph", "max_total_chars": -1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                review_files([FileDiff("a.py", "diff")], **kwargs)
        model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
