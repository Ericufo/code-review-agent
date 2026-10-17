from github_client import GitHubClient
from github_review import PullRequestRevision, ensure_current_revision


BOT_MARKER = "<!-- code-review-agent -->"


class GitHubCommentManager:
    def __init__(self, client: GitHubClient):
        self.client = client

    def publish(
        self,
        pr_number: int,
        body: str,
        *,
        revision: PullRequestRevision | None = None,
    ) -> str:
        comments = self.client.get_issue_comments(
            number=pr_number,
        )

        # Comment pagination can take time: check again just before writing.
        if revision is not None:
            ensure_current_revision(self.client, pr_number, revision)

        marked_body = (
            f"{BOT_MARKER}\n\n"
            f"{body}"
        )

        for comment in comments:
            comment_body = comment.get("body") or ""

            if BOT_MARKER not in comment_body:
                continue

            comment_id = comment.get("id")

            if comment_id is None:
                continue

            self.client.update_issue_comment(
                comment_id=comment_id,
                body=marked_body,
            )

            return "updated"

        self.client.create_issue_comment(
            number=pr_number,
            body=marked_body,
        )

        return "created"
