import os

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()


def review_diff(diff: str) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY")

    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )

    response = client.responses.create(
        model="deepseek-flash",
        instructions=(
            "You are a code reviewer. Review the changes shown in the Git diff. "
            "Focus on bugs, correctness issues, edge cases, maintainability, "
            "and important engineering problems. "
            "Distinguish added lines from removed lines and review the new behavior "
            "rather than treating the diff as a complete source file."
        ),
        input=f"Review this Git diff:\n\n{diff}",
    )

    if not response.output_text:
        raise RuntimeError("LLM returned an empty response")

    return response.output_text