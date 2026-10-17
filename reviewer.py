import os
import json

from openai import OpenAI

from pydantic import ValidationError
from review_models import ReviewResult


def get_review_schema() -> str:
    schema = ReviewResult.model_json_schema()

    return json.dumps(
        schema,
        indent=2,
        ensure_ascii=False,
    )

def build_review_instructions() -> str:
    schema = get_review_schema()

    return f"""
        You are a code reviewer reviewing a Git diff.

        Focus on concrete issues introduced by the submitted changes:
        - correctness bugs
        - regressions
        - important edge cases
        - security issues when relevant

        Report only actionable defects with a concrete trigger and consequence
        supported by the supplied code. Do not report stylistic preferences,
        optional refactors, or hypothetical future callers as defects.
        Do not claim that an external release, tag, or API does not exist based
        only on remembered version information; you have no live lookup tool.
        Missing tests alone are not a defect unless you can identify the actual
        incorrect behavior that needs a regression test.

        Do not assume that code not shown in the diff does not exist.
        Do not report hypothetical issues merely because implementation details
        outside the diff are unavailable.

        If repository context would be required to verify a concern, do not present
        that concern as a confirmed defect.

        Return ONLY valid JSON matching the following JSON Schema:

        {schema}

        If there are no concrete issues, return an empty issues list.
        Set "summary" to an empty string. The application builds one overall
        summary from the collected findings; do not summarize each file.
        Keep each title short, the description to one or two concise sentences
        explaining the trigger and impact, and the suggestion to one sentence.

        The "line" field should refer to the new version of the file when possible.
        Use null if the line cannot be determined.

        Do not include Markdown fences or any text outside the JSON.
        """

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
        instructions=build_review_instructions(),
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
