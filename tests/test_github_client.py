import unittest
from unittest.mock import patch, Mock, call

import requests

from github_client import GitHubClient, GitHubAPIError
from github_comment import BOT_MARKER, GitHubCommentManager


class TestGitHubClient(unittest.TestCase):

    @patch.dict(
        "os.environ",
        {
            "GITHUB_TOKEN": "fake-token",
            "GITHUB_OWNER": "Ericufo",
            "GITHUB_REPO": "code-review-agent",
        },
    )
    @patch("github_client.requests.post")
    def test_create_issue_comment(self, mock_post):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "id": 123,
            "body": "test comment",
        }

        mock_post.return_value = mock_response

        client = GitHubClient()

        result = client.create_issue_comment(
            number=1,
            body="test comment",
        )

        self.assertEqual(result["id"], 123)

        mock_post.assert_called_once_with(
            "https://api.github.com/repos/Ericufo/code-review-agent/issues/1/comments",
            headers=client.headers,
            json={
                "body": "test comment",
            },
            timeout=10,
        )

    @patch.dict(
        "os.environ",
        {
            "GITHUB_TOKEN": "fake-token",
            "GITHUB_OWNER": "Ericufo",
            "GITHUB_REPO": "code-review-agent",
        },
    )
    @patch("github_client.requests.post")
    def test_create_issue_comment_api_failure(self, mock_post):
        mock_response = Mock()

        mock_response.raise_for_status.side_effect = (
            requests.RequestException(
                "API failure"
            )
        )

        mock_post.return_value = mock_response

        client = GitHubClient()

        with self.assertRaises(GitHubAPIError):
            client.create_issue_comment(
                number=1,
                body="test",
            )

    @patch.dict(
        "os.environ",
        {
            "GITHUB_TOKEN": "fake-token",
            "GITHUB_OWNER": "Ericufo",
            "GITHUB_REPO": "code-review-agent",
        },
    )
    @patch("github_client.requests.get")
    def test_get_issue_comments(self, mock_get):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [
            {
                "id": 123,
                "body": "test comment",
            }
        ]
        mock_response.links = {}

        mock_get.return_value = mock_response

        client = GitHubClient()

        result = client.get_issue_comments(
            number=1
        )

        self.assertEqual(
            result[0]["id"],
            123,
        )

        mock_get.assert_called_once_with(
            "https://api.github.com/repos/Ericufo/code-review-agent/issues/1/comments?per_page=100",
            headers=client.headers,
            timeout=10,
        )

    @patch.dict(
        "os.environ",
        {
            "GITHUB_TOKEN": "fake-token",
            "GITHUB_OWNER": "Ericufo",
            "GITHUB_REPO": "code-review-agent",
        },
    )
    @patch("github_client.requests.patch")
    def test_update_issue_comment(self, mock_patch):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "id": 123,
            "body": "updated review",
        }

        mock_patch.return_value = mock_response

        client = GitHubClient()

        result = client.update_issue_comment(
            comment_id=123,
            body="updated review",
        )

        self.assertEqual(
            result["body"],
            "updated review",
        )

        mock_patch.assert_called_once_with(
            "https://api.github.com/repos/Ericufo/code-review-agent/issues/comments/123",
            headers=client.headers,
            json={
                "body": "updated review",
            },
            timeout=10,
        )


@patch.dict("os.environ", {
    "GITHUB_TOKEN": "fake-token",
    "GITHUB_OWNER": "Ericufo",
    "GITHUB_REPO": "code-review-agent",
})
class TestCommentPagination(unittest.TestCase):
    @patch.dict("os.environ", {"GITHUB_COMMENT_AUTHOR": "github-actions[bot]"})
    @patch("github_client.requests.patch")
    @patch("github_client.requests.post")
    @patch("github_client.requests.get")
    def test_updates_bot_comment_on_later_page(self, mock_get, mock_post, mock_patch):
        url = "https://api.github.com/repos/Ericufo/code-review-agent/issues/7/comments"
        next_url = f"{url}?per_page=100&page=2"
        first = Mock()
        first.json.return_value = [{"id": 10, "body": "Human comment"}]
        first.links = {"next": {"url": next_url}}
        last = Mock()
        last.json.return_value = [{
            "id": 123, "body": BOT_MARKER, "user": {"login": "github-actions[bot]"},
        }]
        last.links = {}
        mock_get.side_effect = [first, last]
        client = GitHubClient()

        action = GitHubCommentManager(client).publish(7, "Latest review")

        self.assertEqual(action, "updated")
        self.assertEqual(mock_get.call_args_list, [
            call(f"{url}?per_page=100", headers=client.headers, timeout=10),
            call(next_url, headers=client.headers, timeout=10),
        ])
        mock_post.assert_not_called()
        mock_patch.assert_called_once_with(
            "https://api.github.com/repos/Ericufo/code-review-agent/issues/comments/123",
            headers=client.headers,
            json={"body": f"{BOT_MARKER}\n\nLatest review"},
            timeout=10,
        )

    @patch("github_client.requests.patch")
    @patch("github_client.requests.post")
    @patch("github_client.requests.get")
    def test_later_page_failure_does_not_create_duplicate(
        self, mock_get, mock_post, mock_patch
    ):
        first = Mock()
        first.json.return_value = [{"id": 10, "body": "Human comment"}]
        first.links = {"next": {"url": "https://api.github.com/next-page"}}
        mock_get.side_effect = [first, requests.Timeout("timeout")]

        with self.assertRaises(GitHubAPIError):
            GitHubCommentManager(GitHubClient()).publish(7, "Latest review")

        mock_post.assert_not_called()
        mock_patch.assert_not_called()


@patch.dict("os.environ", {
    "GITHUB_TOKEN": "fake-token",
    "GITHUB_OWNER": "Ericufo",
    "GITHUB_REPO": "code-review-agent",
})
class TestPRFilePagination(unittest.TestCase):
    url = "https://api.github.com/repos/Ericufo/code-review-agent/pulls/7"

    @staticmethod
    def response(data, next_url=None):
        response = Mock()
        response.json.return_value = data
        response.links = {"next": {"url": next_url}} if next_url else {}
        return response

    @patch("github_client.requests.get")
    def test_fetches_every_file_page(self, mock_get):
        files = [{"filename": f"file_{index}.py", "patch": "diff"} for index in range(105)]
        next_url = f"{self.url}/files?per_page=100&page=2"
        mock_get.side_effect = [
            self.response({"changed_files": 105}),
            self.response(files[:100], next_url),
            self.response(files[100:]),
        ]
        client = GitHubClient()

        result = client.get_pull_request_files(7)

        self.assertEqual(result, files)
        self.assertEqual(mock_get.call_args_list, [
            call(self.url, headers=client.headers, timeout=10),
            call(f"{self.url}/files?per_page=100", headers=client.headers, timeout=10),
            call(next_url, headers=client.headers, timeout=10),
        ])

    @patch("github_client.requests.get")
    def test_second_page_failure_raises_instead_of_returning_first_page(self, mock_get):
        mock_get.side_effect = [
            self.response({"changed_files": 2}),
            self.response([{"filename": "first.py"}], f"{self.url}/files?page=2"),
            requests.Timeout("page 2 timeout"),
        ]

        with self.assertRaises(GitHubAPIError):
            GitHubClient().get_pull_request_files(7)
        self.assertEqual(mock_get.call_count, 3)

    @patch("github_client.requests.get")
    def test_mismatched_file_count_is_not_accepted_as_complete(self, mock_get):
        mock_get.side_effect = [
            self.response({"changed_files": 2}),
            self.response([{"filename": "first.py"}]),
        ]

        with self.assertRaisesRegex(GitHubAPIError, "returned 1/2 changed files"):
            GitHubClient().get_pull_request_files(7)

    @patch("github_client.requests.get")
    def test_pr_over_github_file_limit_is_rejected(self, mock_get):
        mock_get.return_value = self.response({"changed_files": 3001})

        with self.assertRaisesRegex(GitHubAPIError, "at most 3000"):
            GitHubClient().get_pull_request_files(7)
        mock_get.assert_called_once()

    @patch("github_client.requests.get")
    def test_empty_pr_returns_empty_list(self, mock_get):
        mock_get.side_effect = [
            self.response({"changed_files": 0}), self.response([]),
        ]

        self.assertEqual(GitHubClient().get_pull_request_files(7), [])

    @patch("github_client.requests.get")
    def test_invalid_file_count_is_not_assumed_to_be_zero(self, mock_get):
        for value in (None, "2", -1, True):
            with self.subTest(value=value):
                mock_get.return_value = self.response({"changed_files": value})
                with self.assertRaisesRegex(GitHubAPIError, "valid changed-file count"):
                    GitHubClient().get_pull_request_files(7)


@patch.dict("os.environ", {
    "GITHUB_TOKEN": "fake-token", "GITHUB_OWNER": "owner", "GITHUB_REPO": "repo",
}, clear=True)
class TestCommentAuthor(unittest.TestCase):
    @patch("github_client.requests.get")
    def test_manual_token_resolves_its_user(self, mock_get):
        mock_get.return_value.json.return_value = {"login": "Ericufo"}
        client = GitHubClient()

        self.assertEqual(client.get_comment_author_login(), "Ericufo")
        mock_get.assert_called_once_with(
            "https://api.github.com/user", headers=client.headers, timeout=10,
        )

    @patch.dict("os.environ", {"GITHUB_COMMENT_AUTHOR": "github-actions[bot]"})
    @patch("github_client.requests.get")
    def test_actions_author_does_not_use_user_endpoint(self, mock_get):
        self.assertEqual(GitHubClient().get_comment_author_login(), "github-actions[bot]")
        mock_get.assert_not_called()

    @patch.dict("os.environ", {"GITHUB_COMMENT_AUTHOR": "   "})
    @patch("github_client.requests.get")
    def test_blank_explicit_author_fails_without_fallback(self, mock_get):
        with self.assertRaisesRegex(GitHubAPIError, "comment author"):
            GitHubClient().get_comment_author_login()
        mock_get.assert_not_called()

    @patch("github_client.requests.get")
    def test_missing_author_is_not_treated_as_a_match(self, mock_get):
        for data in ({}, {"login": None}, {"login": ""}, {"login": 123}, []):
            with self.subTest(data=data):
                mock_get.return_value.json.return_value = data
                with self.assertRaisesRegex(GitHubAPIError, "comment author"):
                    GitHubClient().get_comment_author_login()


if __name__ == "__main__":
    unittest.main()
