import os

from dotenv import load_dotenv
from openai import OpenAI

from pydantic import ValidationError
from review_models import ReviewResult

load_dotenv()


def review_diff(diff: str) -> ReviewResult:
    api_key = os.getenv("DEEPSEEK_API_KEY")

    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )

    response = client.responses.create(
        model="deepseek-flash",
        instructions=( """
            You are a code reviewer reviewing a Git diff.

            Focus on concrete issues introduced by the submitted changes:
            - correctness bugs
            - regressions
            - important edge cases
            - maintainability problems
            - security issues when relevant

            Do not assume that code not shown in the diff does not exist.
            Do not report hypothetical issues merely because implementation details
            outside the diff are unavailable.

            If repository context would be required to verify a concern, do not present
            that concern as a confirmed defect.

            Return ONLY valid JSON in exactly this structure:

            {
                "summary": "brief overall assessment",
                "issues": [
                    {
                    "severity": "critical | high | medium | low",
                    "file": "path/to/file.py",
                    "line": 123,
                    "title": "short issue title",
                    "description": "clear explanation",
                    "suggestion": "recommended fix"
                    }
                ]
            }

            If there are no concrete issues, return an empty issues list.

            The "line" field should refer to the new version of the file when possible.
            Use null if the line cannot be determined.

            Do not include Markdown fences or any text outside the JSON.
            """
        ),
        input=f"""
        <git_diff>
        {diff}
        </git_diff>
        """,
    )

    if not response.output_text:
        raise RuntimeError("LLM returned an empty response")

    try:
        return ReviewResult.model_validate_json(response.output_text)
    except ValidationError as exc:
        raise RuntimeError(
            f"Model returned an invalid review response: {exc}"
        ) from exc
