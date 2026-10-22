from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SEVERITY_ORDER = ("critical", "high", "medium", "low")


class ContextEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    repository: str
    revision: str
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode='after')
    def valid_range(self):
        if self.end_line < self.start_line:
            raise ValueError('Evidence range must be ordered')
        return self


class ReviewIssue(BaseModel):
    severity: Literal["critical", "high", "medium", "low"]
    file: str
    line: int | None = Field(default=None, ge=1)
    title: str
    description: str
    suggestion: str | None = None
    evidence: list[ContextEvidence] = Field(default_factory=list)


class ReviewResult(BaseModel):
    summary: str
    issues: list[ReviewIssue]


class ContextReviewIssue(ReviewIssue):
    evidence: list[ContextEvidence] = Field(min_length=1)


class ContextReviewResult(ReviewResult):
    issues: list[ContextReviewIssue]
    context_complete: bool = Field(strict=True)
    context_reason: str
