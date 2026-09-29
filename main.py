import os

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

def read_code() -> str:
    print("Paste the code to review.")
    print("Enter END on a new line when finished:")

    lines = []

    while True:
        line = input()
        if line == "END":
            break
        lines.append(line)

    return "\n".join(lines)


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

    code = read_code()

    result = review_code(code)

    print()
    print("=== Review Result ===")
    print(result)

if __name__ == "__main__":
    main()
