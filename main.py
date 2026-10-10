import sys
import subprocess

from git_utils import get_git_diff
from review_models import ReviewResult
from diff_parser import parse_git_diff
from review_pipeline import review_files

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

    if len(sys.argv) != 1:
        print("Usage: python main.py", file=sys.stderr)
        return 2

    try:
        diff = get_git_diff()
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() if exc.stderr else str(exc)
        print(f"Git error: {message}", file=sys.stderr)
        return 1

    if not diff.strip():
        print("No changes to review.")
        return 0

    print("=== Code Review Agent V1 ===")
    print("Reviewing current Git changes...")
    
    try:
        file_diffs = parse_git_diff(diff)

        if not file_diffs:
            print("No changed files to review.")
            return 0

        print(f"Reviewing {len(file_diffs)} changed file(s)...")

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
