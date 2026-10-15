import contextlib
import io
import unittest
from unittest.mock import Mock, patch

import main
from diff_parser import FileDiff
from github_client import GitHubAPIError, GitHubClient
from github_comment import BOT_MARKER
from review_models import ReviewResult


class TestMain(unittest.TestCase):
    def setUp(self):
        self.client = Mock(spec=GitHubClient)
        self.client.get_pull_request_files.return_value = [
            {"filename": "example.py", "patch": "@@ -1 +1 @@\n-old\n+new"}
        ]
        self.client.get_issue_comments.return_value = []

        client_patch = patch("main.GitHubClient", return_value=self.client)
        self.client_factory = client_patch.start()
        self.addCleanup(client_patch.stop)

        review_patch = patch(
            "review_pipeline.review_diff",
            return_value=ReviewResult(summary="First review", issues=[]),
        )
        self.review = review_patch.start()
        self.addCleanup(review_patch.stop)

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("sys.argv", ["main.py", *args]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = main.main()
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_repeated_pr_review_creates_then_updates_marked_comment(self):
        exit_code, stdout, stderr = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Created review comment", stdout)
        self.client.get_pull_request_files.assert_called_once_with(number=7)
        self.client.create_issue_comment.assert_called_once()
        created = self.client.create_issue_comment.call_args.kwargs
        self.assertEqual(created["number"], 7)
        self.assertTrue(created["body"].startswith(BOT_MARKER))
        self.assertIn("First review", created["body"])

        self.client.get_issue_comments.return_value = [
            {"id": 10, "body": "Human comment"},
            {"id": 123, "body": created["body"]},
        ]
        self.review.return_value = ReviewResult(summary="Latest review", issues=[])

        exit_code, stdout, stderr = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Updated review comment", stdout)
        self.assertEqual(self.client.create_issue_comment.call_count, 1)
        self.client.update_issue_comment.assert_called_once()
        updated = self.client.update_issue_comment.call_args.kwargs
        self.assertEqual(updated["comment_id"], 123)
        self.assertTrue(updated["body"].startswith(BOT_MARKER))
        self.assertIn("Latest review", updated["body"])
        self.assertNotIn("First review", updated["body"])

    def test_comment_failures_return_nonzero_without_success_message(self):
        for operation in (
            "get_issue_comments", "create_issue_comment", "update_issue_comment"
        ):
            with self.subTest(operation=operation):
                self.client.reset_mock(side_effect=True)
                self.client.get_issue_comments.return_value = (
                    [{"id": 123, "body": BOT_MARKER}]
                    if operation == "update_issue_comment" else []
                )
                getattr(self.client, operation).side_effect = GitHubAPIError(
                    "simulated API failure"
                )

                exit_code, stdout, stderr = self.run_cli("--github-pr", "7")

                self.assertEqual(exit_code, 1)
                self.assertIn("Failed to post GitHub comment", stderr)
                self.assertIn("=== Review Result ===", stdout)
                self.assertNotIn("Created review comment", stdout)
                self.assertNotIn("Updated review comment", stdout)
                if operation == "get_issue_comments":
                    self.client.create_issue_comment.assert_not_called()
                    self.client.update_issue_comment.assert_not_called()
                elif operation == "update_issue_comment":
                    self.client.create_issue_comment.assert_not_called()

    def test_legacy_unmarked_comment_is_preserved(self):
        self.client.get_issue_comments.return_value = [
            {"id": 12, "body": "## 🤖 Code Review Agent\nOld V3 review"}
        ]

        exit_code, _, _ = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 0)
        self.client.create_issue_comment.assert_called_once()
        self.client.update_issue_comment.assert_not_called()

    def test_incomplete_review_still_reports_failures_when_publish_fails(self):
        self.review.side_effect = TimeoutError("model timeout")
        self.client.create_issue_comment.side_effect = GitHubAPIError("denied")

        exit_code, _, stderr = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 1)
        self.assertIn("Failed to post GitHub comment", stderr)
        self.assertIn("[FAILED] example.py", stderr)
        self.assertIn("Warning: review is incomplete", stderr)

    def test_incomplete_review_returns_nonzero_after_publishing(self):
        self.review.side_effect = TimeoutError("model timeout")

        exit_code, stdout, stderr = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 1)
        self.assertIn("Created review comment", stdout)
        self.assertIn("Warning: review is incomplete", stderr)
        body = self.client.create_issue_comment.call_args.kwargs["body"]
        self.assertIn("Failed: 1", body)

    def test_local_review_does_not_use_github(self):
        with patch("main.get_local_files", return_value=[FileDiff("local.py", "diff")]):
            exit_code, stdout, stderr = self.run_cli()

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("First review", stdout)
        self.client_factory.assert_not_called()
        self.client.create_issue_comment.assert_not_called()
        self.client.update_issue_comment.assert_not_called()

    def test_no_changes_does_not_review_or_publish(self):
        self.client.get_pull_request_files.return_value = []

        exit_code, stdout, stderr = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("No changes to review", stdout)
        self.review.assert_not_called()
        self.client.get_issue_comments.assert_not_called()
        self.client.create_issue_comment.assert_not_called()
        self.client.update_issue_comment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
