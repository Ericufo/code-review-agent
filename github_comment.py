from github_client import GitHubClient


BOT_MARKER = "<!-- code-review-agent -->"


class GitHubCommentManager:
    def __init__(self, client: GitHubClient):
        self.client = client

    def publish(
        self,
        pr_number: int,
        body: str,
    ) -> str:
        comments = self.client.get_issue_comments(
            number=pr_number,
        )

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
