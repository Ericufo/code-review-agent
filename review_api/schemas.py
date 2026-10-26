"""Public HTTP contracts; database keys and fingerprints never escape."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from review_api.storage_types import ReviewSubmission, StoredConfig, StoredEvent
from review_models import ReviewIssue


class SubmitReview(ReviewSubmission):
    pass


class ErrorResponse(BaseModel):
    code: str
    message: str
    request_id: str


ExecutionStatus = Literal['queued', 'running', 'finished', 'failed', 'interrupted']
ReviewStatus = Literal['COMPLETE', 'INCOMPLETE', 'NO_ELIGIBLE_FILES']


class TaskResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: UUID
    owner: str
    repo: str
    pr_number: int
    profile: str
    head_sha: str
    base_sha: str
    config: StoredConfig
    prompt_id: str
    source_id: str
    model_id: str
    schema_version: int
    execution_status: ExecutionStatus
    review_status: ReviewStatus | None
    publication_state: Literal['disabled']
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    last_event_seq: int
    resource_records_complete: bool
    summary: str | None
    total_files: int | None
    reviewed_files: int | None
    failed_files: int | None
    skipped_files: int | None
    excluded_files: int | None
    partial: bool


class SubmissionResponse(BaseModel):
    task_id: UUID
    model_mode: Literal['mock', 'live']
    execution_status: ExecutionStatus
    head_sha: str
    base_sha: str
    status_url: str
    result_url: str
    events_url: str


class TaskPage(BaseModel):
    items: list[TaskResponse]
    next_cursor: str | None


class FileResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str
    status: Literal['reviewed', 'failed', 'skipped', 'excluded']
    reason_code: str | None
    completed_at: datetime
    duration_ms: float
    findings: list[ReviewIssue]


class ResultResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: UUID
    execution_status: ExecutionStatus
    model_mode: Literal['mock', 'live']
    resource_records_complete: bool
    partial: bool
    review_status: ReviewStatus | None
    head_sha: str
    base_sha: str
    summary: str | None
    files: list[FileResponse]
    saved_file_counts: dict[str, int]


class EventResponse(StoredEvent):
    task_id: UUID
    seq: int


class EventPage(BaseModel):
    items: list[EventResponse]
    has_more: bool
    next_after_seq: int


class ListQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=256)


class EventQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    after_seq: int = Field(default=0, ge=0, le=9_223_372_036_854_775_807)
    limit: int = Field(default=100, ge=1, le=500)
