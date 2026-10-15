import os
import requests

class GitHubAPIError(Exception):
    pass

class GitHubClient:
    def __init__(self, token=None):
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.owner = os.getenv("GITHUB_OWNER")
        self.repo = os.getenv("GITHUB_REPO")

        if not self.token:
            raise ValueError(
                "GitHub token is required. Set GITHUB_TOKEN environment variable."
            )

        if not self.owner:
            raise ValueError(
                "GitHub owner is required"
            )

        if not self.repo:
            raise ValueError(
                "GitHub repo is required. "
                "Set GITHUB_REPO environment variable."
            )

        self.base_url = "https://api.github.com"

    @property
    def headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json"
        }

    def _get(self, url):
        try:
            response = requests.get(
                url,
                headers=self.headers,
                timeout=10,
            )

            response.raise_for_status()

        except requests.RequestException as exc:
            status = (
                exc.response.status_code
                if exc.response is not None
                else "unknown"
            )

            raise GitHubAPIError(
                f"GitHub API request failed "
                f"(status={status})"
            ) from exc

        return response.json()


    def get_pull_request(self, number):
        url = (
            f"{self.base_url}/repos/"
            f"{self.owner}/{self.repo}"
            f"/pulls/{number}"
        )

        return self._get(url)


    def get_pull_request_files(self, number):
        url = (
            f"{self.base_url}/repos/"
            f"{self.owner}/{self.repo}"
            f"/pulls/{number}/files"
        )

        return self._get(url)

    def get_issue_comments(self, number):
        url = (
            f"{self.base_url}/repos/"
            f"{self.owner}/{self.repo}"
            f"/issues/{number}/comments"
        )

        return self._get(url)

    def _post(self, url, payload):
        try:
            response = requests.post(
                url,
                headers=self.headers,
                json=payload,
                timeout=10,
            )

            response.raise_for_status()

        except requests.RequestException as exc:
            status = (
                exc.response.status_code
                if exc.response is not None
                else "unknown"
            )

            raise GitHubAPIError(
                f"GitHub API request failed "
                f"(status={status})"
            ) from exc

        return response.json()

    def create_issue_comment(self, number, body):
        url = (
            f"{self.base_url}/repos/"
            f"{self.owner}/{self.repo}"
            f"/issues/{number}/comments"
        )

        payload = {
            "body": body
        }

        return self._post(url, payload)

    def _patch(self, url, payload):
        try:
            response = requests.patch(
                url,
                headers=self.headers,
                json=payload,
                timeout=10,
            )

            response.raise_for_status()

        except requests.RequestException as exc:
            status = (
                exc.response.status_code
                if exc.response is not None
                else "unknown"
            )

            raise GitHubAPIError(
                f"GitHub API request failed "
                f"(status={status})"
            ) from exc

        return response.json()

    def update_issue_comment(self, comment_id, body):
        url = (
            f"{self.base_url}/repos/"
            f"{self.owner}/{self.repo}"
            f"/issues/comments/{comment_id}"
        )

        payload = {
            "body": body
        }

        return self._patch(url, payload)
