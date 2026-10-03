import sys
from pathlib import Path

from reviewer import review_code


def read_code(path: Path) -> str:
    return path.read_text(encoding="utf-8")

def main() -> int:
    print("=== Code Review Agent V0 ===")

    if len(sys.argv) != 2:
        print("Usage: python main.py <file>", file=sys.stderr)
        return 2

    path = Path(sys.argv[1])

    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 1

    if not path.is_file():
        print(f"Error: not a file: {path}", file=sys.stderr)
        return 1

    try:
        code = read_code(path)
    except (OSError, UnicodeDecodeError) as error:
        print(f"Error reading {path}: {error}", file=sys.stderr)
        return 1

    print(f"Reviewing: {path}")
    print()
    
    try:
        result = review_code(code)
    except Exception as error:
        print(f"Error: code review failed: {error}", file=sys.stderr)
        return 1

    print("=== Review Result ===")
    print(result)

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
