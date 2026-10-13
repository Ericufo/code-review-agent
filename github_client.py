import os
import requests


class GitHubClient:
    def __init__(self, token=None):
        self.token = token or os.getenv("GITHUB_TOKEN")

        if not self.token:
            raise ValueError(
                "GitHub token is required. Set GITHUB_TOKEN environment variable."
            )

        self.base_url = "https://api.github.com"

    def get_pull_request(self, owner, repo, number):
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{number}"

        response = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json"
            },
            timeout=10
        )

        response.raise_for_status()
        return response.json()


    def get_pull_request_files(self, owner, repo, number):
        url = (
            f"{self.base_url}/repos/"
            f"{owner}/{repo}/pulls/{number}/files"
        )

        response = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
        )

        response.raise_for_status()

        return response.json()
