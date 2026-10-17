import sys
import subprocess
import argparse

from dotenv import load_dotenv
load_dotenv()

from git_utils import get_git_diff
from diff_parser import FileDiff, parse_git_diff
from review_pipeline import ReviewBatch, review_files
from github_client import GitHubClient, GitHubAPIError
from github_comment import GitHubCommentManager
from github_diff import github_files_to_diff
from github_review import (
    PullRequestRevision,
    ReviewSupersededError,
    ensure_current_revision,
    get_review_revision,
)
from review_formatter import format_review_batch


def get_local_files() -> list[FileDiff]:
    try:
        diff = get_git_diff()
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() if exc.stderr else str(exc)
        raise RuntimeError(f"Git error: {message}")

    if not diff.strip():
        return []

    return parse_git_diff(diff)


def get_github_files(client: GitHubClient, pr_number: int) -> list[FileDiff]:
    files = client.get_pull_request_files(
        number=pr_number,
    )

    return github_files_to_diff(files)

def parse_args():
    parser = argparse.ArgumentParser(
        description="Code Review Agent"
    )

    parser.add_argument(
        "--github-pr",
        type=int,
        help="Review a GitHub pull request"
    )

    parser.add_argument(
        "--expected-head-sha",
        help="Skip an outdated Actions run if the PR head has changed",
    )

    args = parser.parse_args()
    if args.expected_head_sha is not None and args.github_pr is None:
        parser.error("--expected-head-sha requires --github-pr")
    return args

def post_github_comment(
    client: GitHubClient,
    pr_number: int,
    batch: ReviewBatch,
    revision: PullRequestRevision,
) -> str:
    body = format_review_batch(batch)
    body += (
        f"\n\nReviewed commit: `{revision.head_sha}`\n\n"
        f"Base commit: `{revision.base_sha}`"
    )

    return GitHubCommentManager(client).publish(
        pr_number=pr_number,
        body=body,
        revision=revision,
    )

def print_review(batch: ReviewBatch) -> None:
    result = batch.result
    print()
    print("=== Review Result ===")
    status = "INCOMPLETE" if batch.incomplete else "COMPLETE"
    print(f"Status: {status}")
    print(batch.coverage_summary)
    print(result.summary)

    if not result.issues:
        return

    for index, issue in enumerate(result.issues, start=1):
        print()
        print(
            f"{index}. [{issue.severity.upper()}] "
            f"{issue.title}"
        )

        location = issue.file

        if issue.line is not None:
            location += f":{issue.line}"

        print(f"   Location: {location}")
        print(f"   {issue.description}")

        if issue.suggestion:
            print(f"   Suggestion: {issue.suggestion}")


def main() -> int:
    args = parse_args()
    client = None

    try:
        if args.github_pr is not None:
            client = GitHubClient()
            revision = get_review_revision(
                client, args.github_pr, expected_head_sha=args.expected_head_sha,
            )
            file_diffs = get_github_files(client, args.github_pr)
            # Reject a mixed file list even if its file count stayed the same.
            ensure_current_revision(client, args.github_pr, revision)
            source = f"GitHub PR #{args.github_pr}"
        else:
            file_diffs = get_local_files()
            source = "current Git changes"

    except ReviewSupersededError as exc:
        print(f"Review skipped: {exc}")
        return 0

    except GitHubAPIError as exc:
        print(
            f"GitHub error: {exc}",
            file=sys.stderr,
        )
        return 1

    except Exception as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 1

    if not file_diffs and client is None:
        print("No changes to review.")
        return 0

    print("=== Code Review Agent V3 ===")
    print(f"Reviewing {source}...")
    print(f"Reviewing {len(file_diffs)} changed file(s)...")
    if client is not None:
        print(f"Reviewed commit: {revision.head_sha}")
        print(f"Base commit: {revision.base_sha}")

    try:
        batch = review_files(file_diffs)

    except Exception as exc:
        print(f"Review failed: {exc}", file=sys.stderr)
        return 1

    print_review(batch)

    publish_failed = False
    if args.github_pr is not None:
        try:
            action = post_github_comment(
                client,
                args.github_pr,
                batch,
                revision,
            )
            print(f"{action.capitalize()} review comment on GitHub PR.")

        except ReviewSupersededError as exc:
            print(f"Review skipped: {exc}")
            return 0

        except GitHubAPIError as exc:
            publish_failed = True
            print(
                f"Failed to post GitHub comment: {exc}",
                file=sys.stderr,
            )

    for problem in batch.failures:
        print(
            f"[FAILED] {problem.path}: {problem.reason}",
            file=sys.stderr,
        )

    for problem in batch.skipped:
        print(
            f"[SKIPPED] {problem.path}: {problem.reason}",
            file=sys.stderr,
        )

    if batch.incomplete:
        print(
            "Warning: review is incomplete.",
            file=sys.stderr,
        )
        return 1

    return 1 if publish_failed else 0

if __name__ == "__main__":
    raise SystemExit(main())
