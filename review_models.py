from typing import Literal

from pydantic import BaseModel, Field


class ReviewIssue(BaseModel):
    severity: Literal["critical", "high", "medium", "low"]
    file: str
    line: int | None = Field(default=None, ge=1)
    title: str
    description: str
    suggestion: str | None = None


class ReviewResult(BaseModel):
    summary: str
    issues: list[ReviewIssue]