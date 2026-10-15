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
        last.json.return_value = [{"id": 123, "body": BOT_MARKER}]
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


if __name__ == "__main__":
    unittest.main()
