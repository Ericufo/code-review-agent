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

        self.assertEqual(
            result["id"],
            123,
        )

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


if __name__ == "__main__":
    unittest.main()
