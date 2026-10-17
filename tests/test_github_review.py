import unittest
from unittest.mock import Mock

from github_client import GitHubAPIError, GitHubClient
from github_review import (
    PullRequestRevision,
    ReviewSupersededError,
    ensure_current_revision,
    get_review_revision,
)


class TestPullRequestRevision(unittest.TestCase):
    def setUp(self):
        self.client = Mock(spec=GitHubClient)
        self.metadata = {
            "state": "open", "draft": False,
            "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40},
        }
        self.client.get_pull_request.return_value = self.metadata

    def test_reads_both_head_and_base(self):
        self.assertEqual(
            get_review_revision(self.client, 7, expected_head_sha="a" * 40),
            PullRequestRevision("a" * 40, "b" * 40),
        )
        self.client.get_pull_request.assert_called_once_with(number=7)

    def test_closed_or_draft_pr_is_skipped(self):
        for changes in ({"state": "closed"}, {"draft": True}):
            with self.subTest(changes=changes):
                self.client.get_pull_request.return_value = {**self.metadata, **changes}
                with self.assertRaises(ReviewSupersededError):
                    get_review_revision(self.client, 7)

    def test_missing_state_is_an_error_not_a_successful_skip(self):
        self.client.get_pull_request.return_value = {**self.metadata, "state": None}
        with self.assertRaisesRegex(GitHubAPIError, "valid PR state"):
            get_review_revision(self.client, 7)

    def test_missing_or_malformed_revision_is_rejected(self):
        for side in ("head", "base"):
            for value in (None, [], {}, {"sha": None}, {"sha": ""}, {"sha": "not-a-sha"}):
                with self.subTest(side=side, value=value):
                    self.client.get_pull_request.return_value = {**self.metadata, side: value}
                    with self.assertRaisesRegex(GitHubAPIError, f"valid PR {side} SHA"):
                        get_review_revision(self.client, 7)

    def test_same_head_but_new_base_is_also_superseded(self):
        revision = get_review_revision(self.client, 7)
        self.client.get_pull_request.return_value = {
            **self.metadata, "base": {"sha": "c" * 40},
        }
        with self.assertRaises(ReviewSupersededError):
            ensure_current_revision(self.client, 7, revision)

    def test_api_failure_propagates(self):
        self.client.get_pull_request.side_effect = GitHubAPIError("unavailable")
        with self.assertRaisesRegex(GitHubAPIError, "unavailable"):
            get_review_revision(self.client, 7)


if __name__ == "__main__":
    unittest.main()
