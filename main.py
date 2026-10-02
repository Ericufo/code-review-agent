import sys
from pathlib import Path

from reviewer import review_code


def read_code(path: Path) -> str:
    return path.read_text(encoding="utf-8")

def main():
    print("=== Code Review Agent V0 ===")

    if len(sys.argv) != 2:
        print("Usage: python main.py <file>")
        return

    path = Path(sys.argv[1])

    if not path.exists():
        print(f"Error: file not found: {path}")
        return

    if not path.is_file():
        print(f"Error: not a file: {path}")
        return

    code = read_code(path)

    print(f"Reviewing: {path}")
    print()
    print("=== Review Result ===")

    result = review_code(code)

    print(result)

if __name__ == "__main__":
    main()
