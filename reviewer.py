import os

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)


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