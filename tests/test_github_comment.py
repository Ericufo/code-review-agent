import unittest
from unittest.mock import Mock

from github_client import GitHubClient
from github_review import PullRequestRevision, ReviewSupersededError
from github_comment import (
    BOT_MARKER,
    GitHubCommentManager,
)


class TestGitHubCommentManager(unittest.TestCase):

    def setUp(self):
        self.client = Mock(spec=GitHubClient)
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

    def test_revision_is_checked_after_comment_pagination_before_any_write(self):
        for comments in ([], [{"id": 123, "body": BOT_MARKER}]):
            with self.subTest(comments=comments):
                self.client.reset_mock()
                self.client.get_pull_request.return_value = {
                    "state": "open", "draft": False,
                    "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40},
                }

                def list_comments(number):
                    # Simulate a push while fetching the comment pages.
                    self.client.get_pull_request.return_value["head"]["sha"] = "c" * 40
                    return comments

                self.client.get_issue_comments.side_effect = list_comments

                with self.assertRaises(ReviewSupersededError):
                    self.manager.publish(
                        7, "Old result", revision=PullRequestRevision("a" * 40, "b" * 40),
                    )

                self.client.create_issue_comment.assert_not_called()
                self.client.update_issue_comment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
