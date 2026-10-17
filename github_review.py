"""Identify the PR revision being reviewed and reject obsolete results."""

from dataclasses import dataclass
import re

from github_client import GitHubAPIError, GitHubClient


class ReviewSupersededError(Exception):
    """The PR no longer needs the review started by this run."""


@dataclass(frozen=True)
class PullRequestRevision:
    head_sha: str
    base_sha: str


def get_review_revision(
    client: GitHubClient,
    pr_number: int,
    *,
    expected_head_sha: str | None = None,
) -> PullRequestRevision:
    pull_request = client.get_pull_request(number=pr_number)
    state = pull_request.get("state")
    if state == "closed" or pull_request.get("draft") is True:
        raise ReviewSupersededError("PR is closed or a draft; skipping review.")
    if state != "open":
        raise GitHubAPIError("GitHub did not return a valid PR state")

    shas = []
    for side in ("head", "base"):
        ref = pull_request.get(side)
        sha = ref.get("sha") if isinstance(ref, dict) else None
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise GitHubAPIError(f"GitHub did not return a valid PR {side} SHA")
        shas.append(sha)

    revision = PullRequestRevision(head_sha=shas[0], base_sha=shas[1])
    if expected_head_sha is not None and revision.head_sha != expected_head_sha:
        raise ReviewSupersededError(
            "PR head no longer matches the triggering commit; skipping outdated run."
        )
    return revision


def ensure_current_revision(
    client: GitHubClient,
    pr_number: int,
    revision: PullRequestRevision,
) -> None:
    current = get_review_revision(client, pr_number)
    if current != revision:
        raise ReviewSupersededError(
            "PR head or base changed during review; discarding outdated results. "
            "Run the review again for the current revision."
        )
