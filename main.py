import sys
import subprocess

from reviewer import review_diff
from git_utils import get_git_diff


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
        result = review_diff(diff)
    except Exception as exc:
        print(f"Review failed: {exc}", file=sys.stderr)
        return 1

    print()
    print("=== Review Result ===")
    print(result)

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
