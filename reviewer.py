import os

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()


def review_code(code: str) -> str:
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
            "You are a code reviewer. "
            "Review the submitted code for correctness, readability, "
            "and potential bugs. Keep the review concise."
        ),
        input=f"Review this code:\n\n{code}",
    )

    if not response.output_text:
        raise RuntimeError("LLM returned an empty response")

    return response.output_text