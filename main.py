import os
import sys

from openai import OpenAI
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

def read_code(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def review_code(code: str) -> str:
    response = client.responses.create(
        model="deepseek-flash",
        instructions=(
            "You are a code reviewer. "
            "Review the submitted code for correctness, readability, "
            "and potential bugs. Keep the review concise."
        ),
        input=f"Review this code:\n\n{code}",
    )
    
    return response.output_text

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
