
import unittest
from unittest.mock import call, patch

from diff_parser import FileDiff
from review_models import ReviewIssue, ReviewResult
from review_pipeline import review_files


class TestReviewFiles(unittest.TestCase):

    @patch("review_pipeline.review_diff")
    def test_empty_files(self, mock_review):
        batch = review_files([])

        self.assertEqual(batch.result.issues, [])
        self.assertFalse(batch.incomplete)
        self.assertEqual((batch.reviewed_files, batch.total_files), (0, 0))
        mock_review.assert_not_called()

    @patch("review_pipeline.review_diff")
    def test_multiple_files(self, mock_review):
        mock_review.side_effect = [
            ReviewResult(
                summary="A bug found",
                issues=[
                    ReviewIssue(
                        severity="high",
                        file="wrong.py",
                        line=3,
                        title="Bug",
                        description="Incorrect value",
                    )
                ],
            ),
            ReviewResult(
                summary="Looks good",
                issues=[],
            ),
        ]

        batch = review_files([
            FileDiff("main.py", "diff one"),
            FileDiff("reviewer.py", "diff two"),
        ])

        self.assertEqual(
            mock_review.call_args_list,
            [call("diff one"), call("diff two")],
        )
        self.assertEqual(len(batch.result.issues), 1)
        self.assertEqual(batch.result.issues[0].file, "main.py")
        self.assertIn("Reviewed 2/2 file(s)", batch.coverage_summary)
        self.assertEqual((batch.reviewed_files, batch.total_files), (2, 2))
        self.assertFalse(batch.incomplete)
        self.assertEqual(
            batch.result.summary,
            "Found 1 potential issue(s) across 1 file(s): 1 high.",
        )
        self.assertNotIn("Looks good", batch.result.summary)
        self.assertNotIn("reviewer.py", batch.result.summary)

    @patch("review_pipeline.review_diff")
    def test_batch_summary_counts_affected_files_and_orders_findings(self, mock_review):
        def issue(level, title):
            return ReviewIssue(severity=level, file="model-path.py", title=title, description="Defect")

        mock_review.side_effect = [
            ReviewResult(summary="Long file summary", issues=[
                issue("low", "Low issue"), issue("high", "High issue"),
            ]),
            ReviewResult(summary="Another file summary", issues=[issue("critical", "Critical issue")]),
            ReviewResult(summary="Everything looks good", issues=[]),
        ]

        batch = review_files([FileDiff(path, "diff") for path in ("a.py", "b.py", "clean.py")])

        self.assertEqual(mock_review.call_count, 3)
        self.assertEqual([issue.severity for issue in batch.result.issues], ["critical", "high", "low"])
        self.assertEqual(
            batch.result.summary,
            "Found 3 potential issue(s) across 2 file(s): 1 critical, 1 high, 1 low.",
        )
        self.assertNotIn("clean.py", batch.result.summary)

    @patch("review_pipeline.review_diff")
    def test_many_clean_files_produce_one_short_summary_without_extra_model_calls(self, mock_review):
        mock_review.return_value = ReviewResult(summary="This file looks good. " * 100, issues=[])

        batch = review_files([FileDiff(f"file_{i}.py", "diff") for i in range(18)])

        self.assertEqual(mock_review.call_count, 18)
        self.assertEqual(batch.result.summary, "No issues found in the reviewed changes.")
        self.assertEqual(batch.reviewed_files, 18)

    @patch("review_pipeline.review_diff")
    def test_one_failure_does_not_abort(self, mock_review):
        mock_review.side_effect = [
            TimeoutError("timeout"),
            ReviewResult(summary="ok", issues=[]),
        ]

        batch = review_files([
            FileDiff("broken.py", "diff one"),
            FileDiff("fine.py", "diff two"),
        ])

        self.assertEqual(mock_review.call_count, 2)
        self.assertEqual(batch.failures[0].path, "broken.py")
        self.assertIn("No issues found in the successfully reviewed files", batch.result.summary)
        self.assertNotIn("fine.py: ok", batch.result.summary)
        self.assertIn("Reviewed 1/2 file(s)", batch.coverage_summary)
        self.assertTrue(batch.incomplete)

    @patch("review_pipeline.review_diff")
    def test_all_failed_keeps_error_status(self, mock_review):
        mock_review.side_effect = ValueError("bad response")

        batch = review_files([
            FileDiff("broken.py", "diff"),
        ])

        self.assertEqual(batch.result.issues, [])
        self.assertIn("Reviewed 0/1 file(s)", batch.coverage_summary)
        self.assertEqual(len(batch.failures), 1)
        self.assertTrue(batch.incomplete)

    @patch("review_pipeline.review_diff")
    def test_oversized_file_is_skipped(self, mock_review):
        batch = review_files(
            [FileDiff("big.py", "x" * 12)],
            max_file_chars=10,
        )

        mock_review.assert_not_called()
        self.assertEqual(batch.skipped[0].path, "big.py")
        self.assertTrue(batch.incomplete)

    @patch("review_pipeline.review_diff")
    def test_total_budget_is_applied(self, mock_review):
        mock_review.return_value = ReviewResult(
            summary="ok",
            issues=[],
        )

        batch = review_files(
            [
                FileDiff("a.py", "123456"),
                FileDiff("b.py", "12345"),
            ],
            max_total_chars=10,
        )

        mock_review.assert_called_once_with("123456")
        self.assertEqual(batch.skipped[0].path, "b.py")

    @patch("review_pipeline.review_diff")
    def test_binary_file_is_skipped(self, mock_review):
        binary_diff = (
            "diff --git a/image.png b/image.png\n"
            "Binary files a/image.png and b/image.png differ\n"
        )

        batch = review_files([
            FileDiff("image.png", binary_diff),
        ])

        mock_review.assert_not_called()
        self.assertTrue(batch.incomplete)
    
    @patch("review_pipeline.review_diff")
    def test_missing_patch_does_not_consume_review_budget(self, mock_review):
        mock_review.return_value = ReviewResult(summary="ok", issues=[])

        batch = review_files([
            FileDiff("image.png", "", skip_reason="patch unavailable"),
            FileDiff("main.py", "diff"),
        ], max_files=1, max_total_chars=4)

        mock_review.assert_called_once_with("diff")
        self.assertEqual((batch.reviewed_files, batch.total_files), (1, 2))
        self.assertEqual(batch.skipped[0].reason, "patch unavailable")
        self.assertTrue(batch.incomplete)

    @patch("review_pipeline.review_diff")
    def test_mixed_outcomes_are_all_counted(self, mock_review):
        mock_review.side_effect = [
            ReviewResult(summary="ok", issues=[]),
            TimeoutError("model timeout"),
        ]

        batch = review_files([
            FileDiff("image.png", "", skip_reason="patch unavailable"),
            FileDiff("good.py", "diff"),
            FileDiff("failed.py", "diff"),
            FileDiff("large.py", "x" * 11),
            FileDiff("over_budget.py", "diff"),
        ], max_files=2, max_file_chars=10)

        self.assertEqual(mock_review.call_count, 2)
        self.assertEqual(batch.total_files, 5)
        self.assertEqual(batch.reviewed_files, 1)
        self.assertEqual(len(batch.failures), 1)
        self.assertEqual(len(batch.skipped), 3)
        self.assertEqual(
            batch.reviewed_files + len(batch.failures) + len(batch.skipped),
            batch.total_files,
        )
        self.assertIn("maximum 2 LLM calls", batch.skipped[-1].reason)



if __name__ == "__main__":
    unittest.main()
