import sys
import subprocess
import argparse

from dotenv import load_dotenv
load_dotenv()

from git_utils import get_git_diff
from review_models import ReviewResult
from diff_parser import FileDiff, parse_git_diff
from review_pipeline import review_files
from github_client import GitHubClient
from github_diff import github_files_to_diff


def get_local_files() -> list[FileDiff]:
    try:
        diff = get_git_diff()
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() if exc.stderr else str(exc)
        raise RuntimeError(f"Git error: {message}")

    if not diff.strip():
        return []

    return parse_git_diff(diff)


def get_github_files(pr_number: int) -> list[FileDiff]:
    client = GitHubClient()

    files = client.get_pull_request_files(
        owner="Ericufo",
        repo="code-review-agent",
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

    return parser.parse_args()

def print_review(result: ReviewResult) -> None:
    print()
    print("=== Review Result ===")
    print(result.summary)

    if not result.issues:
        print()
        print("No issues found.")
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

    try:
        if args.github_pr is not None:
            file_diffs = get_github_files(args.github_pr)
            source = f"GitHub PR #{args.github_pr}"
        else:
            file_diffs = get_local_files()
            source = "current Git changes"

    except Exception as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 1

    if not file_diffs:
        print("No changes to review.")
        return 0

    print("=== Code Review Agent V2 ===")
    print(f"Reviewing {source}...")
    print(f"Reviewing {len(file_diffs)} changed file(s)...")

    try:
        batch = review_files(file_diffs)

    except Exception as exc:
        print(f"Review failed: {exc}", file=sys.stderr)
        return 1

    print_review(batch.result)

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

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
