import unittest
from unittest.mock import patch, Mock

import requests

from github_client import GitHubClient, GitHubAPIError


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
            "https://api.github.com/repos/Ericufo/code-review-agent/issues/1/comments",
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


if __name__ == "__main__":
    unittest.main()
