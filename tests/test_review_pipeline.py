
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
        self.assertIn("Reviewed 2/2 file(s)", batch.result.summary)
        self.assertFalse(batch.incomplete)

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
        self.assertIn("fine.py: ok", batch.result.summary)
        self.assertIn("Reviewed 1/2 file(s)", batch.result.summary)
        self.assertTrue(batch.incomplete)

    @patch("review_pipeline.review_diff")
    def test_all_failed_keeps_error_status(self, mock_review):
        mock_review.side_effect = ValueError("bad response")

        batch = review_files([
            FileDiff("broken.py", "diff"),
        ])

        self.assertEqual(batch.result.issues, [])
        self.assertIn("Reviewed 0/1 file(s)", batch.result.summary)
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


if __name__ == "__main__":
    unittest.main()
