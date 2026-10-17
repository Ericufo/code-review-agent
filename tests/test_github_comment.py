import unittest
from unittest.mock import Mock, call

from github_client import GitHubAPIError, GitHubClient
from github_review import PullRequestRevision, ReviewSupersededError
from github_comment import (
    BOT_MARKER,
    GitHubCommentManager,
)


class TestGitHubCommentManager(unittest.TestCase):

    def setUp(self):
        self.client = Mock(spec=GitHubClient)
        self.client.get_comment_author_login.return_value = "review-bot"
        self.manager = GitHubCommentManager(
            self.client
        )

    def test_create_comment_when_bot_comment_does_not_exist(self):
        self.client.get_issue_comments.return_value = [
            {
                "id": 100,
                "body": "Human comment",
            }
        ]

        action = self.manager.publish(
            pr_number=1,
            body="Review result",
        )

        self.assertEqual(action, "created")

        self.client.create_issue_comment.assert_called_once_with(
            number=1,
            body=(
                f"{BOT_MARKER}\n\n"
                "Review result"
            ),
        )

        self.client.update_issue_comment.assert_not_called()

    def test_update_existing_bot_comment(self):
        self.client.get_issue_comments.return_value = [
            {
                "id": 100,
                "body": "Human comment",
            },
            {
                "id": 123,
                "user": {"login": "review-bot"},
                "body": (
                    f"{BOT_MARKER}\n\n"
                    "Old review"
                ),
            },
        ]

        action = self.manager.publish(
            pr_number=1,
            body="New review",
        )

        self.assertEqual(action, "updated")

        self.client.update_issue_comment.assert_called_once_with(
            comment_id=123,
            body=(
                f"{BOT_MARKER}\n\n"
                "New review"
            ),
        )

        self.client.create_issue_comment.assert_not_called()

    def test_comment_with_empty_body_is_ignored(self):
        self.client.get_issue_comments.return_value = [
            {
                "id": 100,
                "body": None,
            }
        ]

        action = self.manager.publish(
            pr_number=1,
            body="Review result",
        )

        self.assertEqual(action, "created")

        self.client.create_issue_comment.assert_called_once()
        self.client.update_issue_comment.assert_not_called()
        self.assertEqual(
            self.client.create_issue_comment.call_args.kwargs["body"],
            f"{BOT_MARKER}\n\nReview result",
        )

    def test_quoted_marker_and_other_authors_are_preserved(self):
        comments = [
            {"id": 1, "body": f"I quoted {BOT_MARKER}", "user": {"login": "review-bot"}},
            {"id": 2, "body": f"> {BOT_MARKER}", "user": {"login": "review-bot"}},
            {"id": 3, "body": f"{BOT_MARKER} extra text", "user": {"login": "review-bot"}},
            {"id": 4, "body": f"{BOT_MARKER}\nHuman message", "user": {"login": "human"}},
            {"id": 5, "body": BOT_MARKER, "user": {"login": "other-bot[bot]"}},
            {"id": 6, "body": BOT_MARKER},
            {"id": 7, "body": BOT_MARKER, "user": None},
        ]
        self.client.get_issue_comments.return_value = comments

        self.assertEqual(self.manager.publish(7, "New review"), "created")

        self.client.update_issue_comment.assert_not_called()
        self.client.create_issue_comment.assert_called_once()

    def test_updates_only_the_matching_author_after_quoted_and_foreign_markers(self):
        self.client.get_issue_comments.return_value = [
            {"id": 1, "body": f"Quote: {BOT_MARKER}", "user": {"login": "review-bot"}},
            {"id": 2, "body": BOT_MARKER, "user": {"login": "human"}},
            {"id": 3, "body": f"{BOT_MARKER}\r\nOld review", "user": {"login": "REVIEW-BOT"}},
            {"id": 4, "body": BOT_MARKER, "user": {"login": "review-bot"}},
        ]

        self.assertEqual(self.manager.publish(7, "New review"), "updated")

        self.assertEqual(self.client.update_issue_comment.call_args.kwargs["comment_id"], 3)
        self.client.update_issue_comment.assert_called_once()
        self.client.create_issue_comment.assert_not_called()

    def test_author_lookup_failure_does_not_write_any_comment(self):
        self.client.get_issue_comments.return_value = []
        self.client.get_comment_author_login.side_effect = GitHubAPIError("identity unavailable")

        with self.assertRaisesRegex(GitHubAPIError, "identity unavailable"):
            self.manager.publish(7, "New review")

        self.client.create_issue_comment.assert_not_called()
        self.client.update_issue_comment.assert_not_called()

    def test_revision_is_checked_after_comment_pagination_before_any_write(self):
        for comments in ([], [{"id": 123, "body": BOT_MARKER}]):
            with self.subTest(comments=comments):
                self.client.reset_mock()
                self.client.get_pull_request.return_value = {
                    "state": "open", "draft": False,
                    "head": {"sha": "c" * 40}, "base": {"sha": "b" * 40},
                }
                self.client.get_issue_comments.return_value = comments

                with self.assertRaises(ReviewSupersededError):
                    self.manager.publish(
                        7, "Old result", revision=PullRequestRevision("a" * 40, "b" * 40),
                    )

                self.client.create_issue_comment.assert_not_called()
                self.client.update_issue_comment.assert_not_called()
                # Verify ordering explicitly; do not simulate HTTP snapshots by
                # mutating a dictionary that a buggy implementation might retain.
                self.assertEqual(self.client.mock_calls, [
                    call.get_issue_comments(number=7),
                    call.get_comment_author_login(),
                    call.get_pull_request(number=7),
                ])


if __name__ == "__main__":
    unittest.main()
