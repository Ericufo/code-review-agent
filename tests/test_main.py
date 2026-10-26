import contextlib
import io
import unittest
from unittest.mock import Mock, patch

import main
from diff_parser import FileDiff
from github_client import GitHubAPIError, GitHubClient
from github_comment import BOT_MARKER
from review_models import ReviewIssue, ReviewResult


class TestMain(unittest.TestCase):
    def setUp(self):
        self.client = Mock(spec=GitHubClient)
        self.client.get_comment_author_login.return_value = "github-actions[bot]"
        self.metadata = {
            "state": "open", "draft": False,
            "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40},
        }
        self.client.get_pull_request.return_value = self.metadata
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
        self.review.return_value = ReviewResult(summary="Per-file prose", issues=[ReviewIssue(
            severity="high", file="example.py", title="First finding", description="First defect",
        )])
        exit_code, stdout, stderr = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Created review comment", stdout)
        self.client.get_pull_request_files.assert_called_once_with(number=7)
        self.client.create_issue_comment.assert_called_once()
        created = self.client.create_issue_comment.call_args.kwargs
        self.assertEqual(created["number"], 7)
        self.assertTrue(created["body"].startswith(BOT_MARKER))
        self.assertIn("First finding", created["body"])
        self.assertNotIn("Per-file prose", created["body"])
        self.assertIn("Status: **COMPLETE**", created["body"])
        self.assertIn("Reviewed 1/1 file(s)", created["body"])
        self.assertIn(f"Reviewed commit: `{'a' * 40}`", created["body"])
        self.assertIn(f"Base commit: `{'b' * 40}`", created["body"])

        self.client.get_issue_comments.return_value = [
            {"id": 10, "body": "Human comment"},
            {"id": 123, "body": created["body"], "user": {"login": "github-actions[bot]"}},
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
        self.assertIn("No issues found in the reviewed changes", updated["body"])
        self.assertNotIn("First finding", updated["body"])
        self.assertNotIn("### Findings", updated["body"])

    def test_pr_no_publish_rechecks_revision_without_comment_calls(self):
        code, stdout, stderr = self.run_cli('--github-pr', '7', '--no-publish')
        self.assertEqual(code, 0)
        self.assertIn('no comment published', stdout)
        self.assertEqual(self.client.get_pull_request.call_count, 3)
        self.client.get_issue_comments.assert_not_called()
        self.client.create_issue_comment.assert_not_called()
        self.client.update_issue_comment.assert_not_called()

    def test_pr_no_publish_discards_superseded_result(self):
        self.client.get_pull_request.side_effect = [self.metadata, self.metadata,
            {**self.metadata, 'head': {'sha': 'c' * 40}}]
        code, stdout, stderr = self.run_cli('--github-pr', '7', '--no-publish')
        self.assertEqual(code, 1)
        self.assertNotIn('=== Review Result ===', stdout)
        self.client.create_issue_comment.assert_not_called()

    def test_comment_failures_return_nonzero_without_success_message(self):
        for operation in (
            "get_issue_comments", "get_comment_author_login", "create_issue_comment", "update_issue_comment"
        ):
            with self.subTest(operation=operation):
                self.client.reset_mock(side_effect=True)
                self.client.get_issue_comments.return_value = (
                    [{"id": 123, "body": BOT_MARKER, "user": {"login": "github-actions[bot]"}}]
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
                if operation in ("get_issue_comments", "get_comment_author_login"):
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
        self.assertIn("example.py: TimeoutError: model timeout", body)
        for output in (stdout, body):
            self.assertIn("No files were successfully reviewed", output)
            self.assertNotIn("No issues found", output)
    
    def test_all_missing_patches_publish_incomplete_review(self):
        self.client.get_pull_request_files.return_value = [
            {"filename": "image.png", "patch": None},
            {"filename": "empty.py", "patch": ""},
        ]

        exit_code, stdout, stderr = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 1)
        self.review.assert_not_called()
        self.client.create_issue_comment.assert_called_once()
        body = self.client.create_issue_comment.call_args.kwargs["body"]
        for output in (stdout, body):
            self.assertIn("Reviewed 0/2 file(s)", output)
            self.assertIn("skipped: 2", output)
            self.assertIn("No conclusion can be drawn", output)
            self.assertNotIn("No changes to review", output)
            self.assertNotIn("No issues found", output)
        for path in ("image.png", "empty.py"):
            self.assertIn(f"{path}: GitHub did not provide", body)
            self.assertIn(f"[SKIPPED] {path}", stderr)

    def test_mixed_results_have_same_coverage_in_cli_and_comment(self):
        self.client.get_pull_request_files.return_value = [
            {"filename": "good.py", "patch": "@@ -1 +1 @@\n-old\n+new"},
            {"filename": "failed.py", "patch": "@@ -1 +1 @@\n-old\n+new"},
            {"filename": "image.png"},
        ]
        self.review.side_effect = [
            ReviewResult(summary="ok", issues=[]), TimeoutError("model timeout"),
        ]

        exit_code, stdout, stderr = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 1)
        self.assertEqual(self.review.call_count, 2)
        body = self.client.create_issue_comment.call_args.kwargs["body"]
        for output in (stdout, body):
            self.assertIn("Reviewed 1/3 file(s). Found 0 issue(s). Failed: 1; skipped: 1.", output)
            self.assertIn("No issues found in the successfully reviewed files", output)
            self.assertNotIn("No issues found.", output)
        self.assertIn("failed.py: TimeoutError: model timeout", body)
        self.assertIn("image.png: GitHub did not provide", body)
        self.assertIn("[FAILED] failed.py", stderr)
        self.assertIn("[SKIPPED] image.png", stderr)

    def test_oversized_patch_is_reported_in_comment(self):
        self.client.get_pull_request_files.return_value = [
            {"filename": "large.py", "patch": "x" * 20_001},
        ]

        exit_code, _, _ = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 1)
        self.review.assert_not_called()
        body = self.client.create_issue_comment.call_args.kwargs["body"]
        self.assertIn("large.py: diff exceeds 20000 characters", body)
        self.assertIn("Reviewed 0/1 file(s)", body)
        self.assertNotIn("No issues found", body)

    def test_file_retrieval_failure_does_not_publish_partial_input(self):
        self.client.get_pull_request_files.side_effect = GitHubAPIError("page 2 failed")

        exit_code, _, stderr = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 1)
        self.assertIn("page 2 failed", stderr)
        self.review.assert_not_called()
        self.client.get_issue_comments.assert_not_called()
        self.client.create_issue_comment.assert_not_called()
        self.client.update_issue_comment.assert_not_called()

    def test_local_binary_diff_reports_incomplete_without_github(self):
        binary = FileDiff("image.png", "Binary files a/image.png and b/image.png differ\n")
        with patch("review_service.get_local_files", return_value=[binary]):
            exit_code, stdout, stderr = self.run_cli()

        self.assertEqual(exit_code, 1)
        self.assertIn("Reviewed 0/1 file(s)", stdout)
        self.assertIn("No conclusion can be drawn", stdout)
        self.assertNotIn("No issues found", stdout)
        self.assertIn("[SKIPPED] image.png: binary diff", stderr)
        self.review.assert_not_called()
        self.client_factory.assert_not_called()

    def test_local_review_does_not_use_github(self):
        with patch("review_service.get_local_files", return_value=[FileDiff("local.py", "diff")]):
            exit_code, stdout, stderr = self.run_cli()

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("No issues found in the reviewed changes", stdout)
        self.assertNotIn("First review", stdout)
        self.client_factory.assert_not_called()
        self.client.create_issue_comment.assert_not_called()
        self.client.update_issue_comment.assert_not_called()

    def test_empty_pr_updates_old_findings_without_calling_model(self):
        self.client.get_pull_request_files.return_value = []
        self.client.get_issue_comments.return_value = [
            {"id": 123, "body": f"{BOT_MARKER}\nOld findings", "user": {"login": "github-actions[bot]"}},
        ]

        exit_code, stdout, stderr = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("No changed files to review", stdout)
        self.review.assert_not_called()
        self.client.create_issue_comment.assert_not_called()
        self.client.update_issue_comment.assert_called_once()
        body = self.client.update_issue_comment.call_args.kwargs["body"]
        self.assertIn("Reviewed 0/0 file(s)", body)
        self.assertNotIn("Old findings", body)

    def test_empty_pr_with_context_budget_still_publishes_empty_summary(self):
        self.client.get_pull_request_files.return_value = []
        exit_code, stdout, stderr = self.run_cli(
            '--github-pr', '7', '--context', 'repo', '--max-model-calls', '64',
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, '')
        self.assertIn('No changed files to review', stdout)
        self.review.assert_not_called()
        self.client.create_issue_comment.assert_called_once()

    def test_empty_local_diff_does_not_review_or_publish(self):
        with patch("review_service.get_local_files", return_value=[]):
            exit_code, stdout, stderr = self.run_cli()
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("No changes to review", stdout)
        self.review.assert_not_called()
        self.client_factory.assert_not_called()

    def assert_no_comment_written(self):
        self.client.create_issue_comment.assert_not_called()
        self.client.update_issue_comment.assert_not_called()

    def test_current_event_head_runs_review(self):
        exit_code, _, stderr = self.run_cli(
            "--github-pr", "7", "--expected-head-sha", "a" * 40,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.review.assert_called_once()
        self.client.create_issue_comment.assert_called_once()

    def test_old_event_is_skipped_before_fetching_files_or_calling_model(self):
        exit_code, stdout, stderr = self.run_cli(
            "--github-pr", "7", "--expected-head-sha", "c" * 40,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("skipping outdated run", stdout)
        self.client.get_pull_request_files.assert_not_called()
        self.review.assert_not_called()
        self.assert_no_comment_written()

    def test_revision_change_during_file_fetch_skips_model(self):
        changed = {**self.metadata, "head": {"sha": "c" * 40}}
        self.client.get_pull_request.side_effect = [self.metadata, changed]

        exit_code, stdout, _ = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 0)
        self.assertIn("discarding outdated results", stdout)
        self.review.assert_not_called()
        self.assert_no_comment_written()

    def test_revision_or_state_change_during_review_never_publishes(self):
        for changes in (
            {"head": {"sha": "c" * 40}},
            {"base": {"sha": "c" * 40}},
            {"state": "closed"},
            {"draft": True},
        ):
            with self.subTest(changes=changes):
                self.client.reset_mock()
                self.review.reset_mock()
                self.client.get_pull_request.side_effect = [
                    self.metadata, self.metadata, {**self.metadata, **changes},
                ]

                exit_code, stdout, _ = self.run_cli("--github-pr", "7")

                self.assertEqual(exit_code, 0)
                self.review.assert_called_once()
                self.assertIn("Review skipped:", stdout)
                self.assertNotIn("Created review comment", stdout)
                self.assert_no_comment_written()

    def test_failed_final_revision_check_does_not_publish(self):
        self.client.get_pull_request.side_effect = [
            self.metadata, self.metadata, GitHubAPIError("revision check failed"),
        ]

        exit_code, stdout, stderr = self.run_cli("--github-pr", "7")

        self.assertEqual(exit_code, 1)
        self.assertIn("revision check failed", stderr)
        self.assertNotIn("Created review comment", stdout)
        self.assert_no_comment_written()

    def test_expected_head_requires_pr_mode(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("--expected-head-sha", "a" * 40)
        self.assertEqual(caught.exception.code, 2)
        self.client_factory.assert_not_called()

    def test_default_engine_and_trace_are_used_by_pr_review(self):
        exit_code, stdout, stderr = self.run_cli("--github-pr", "7", "--trace")
        self.assertEqual(exit_code, 0)
        self.assertIn("Review engine: langgraph", stdout)
        self.assertIn("[GRAPH] review_file", stderr)
        self.assertIn("[GRAPH] summarize", stderr)
        self.assertIn("attempted=1 reviewed=1 failed=0 skipped=0", stderr)
        body = self.client.create_issue_comment.call_args.kwargs["body"]
        self.assertNotIn("[GRAPH]", body)

    def test_classic_engine_is_available_as_baseline(self):
        with patch("review_graph.build_review_graph", side_effect=AssertionError("must not build")):
            exit_code, stdout, stderr = self.run_cli("--github-pr", "7", "--engine", "classic")
        self.assertEqual(exit_code, 0)
        self.assertIn("Review engine: classic", stdout)
        self.assertEqual(stderr, "")
        self.review.assert_called_once()

    def test_trace_with_classic_engine_is_rejected_before_any_work(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("--engine", "classic", "--trace")
        self.assertEqual(caught.exception.code, 2)
        self.review.assert_not_called()
        self.client_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
