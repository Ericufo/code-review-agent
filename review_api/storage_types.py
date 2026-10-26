"""Validated storage payloads; deliberately exclude credentials and raw responses."""
from dataclasses import asdict
import hashlib
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from review_events import ReviewEvent

SHA = Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:/@+-]+$')]
Count = Annotated[int, Field(ge=0)]


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class ReviewSubmission(StrictRecord):
    owner: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9-]{0,38}$')
    repo: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_.-]+$')
    pr_number: int = Field(gt=0, le=2_147_483_647)
    profile: str = Field(default='default', min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')
    expected_head_sha: SHA | None = None

    @field_validator('owner', 'repo')
    @classmethod
    def normalize_repository(cls, value):
        if value in ('.', '..'):
            raise ValueError('Invalid repository name')
        return value.lower()

    def fingerprint(self) -> str:
        value = json.dumps(self.model_dump(), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(value.encode()).hexdigest()


class StoredConfig(StrictRecord):
    engine: Literal['langgraph'] = 'langgraph'
    context_mode: Literal['repo'] = 'repo'
    model_mode: Literal['mock', 'live'] = 'mock'
    max_files: Count = 30
    max_file_chars: Count = 20_000
    max_total_chars: Count = 160_000
    max_model_calls: Count = 64
    max_input_chars: Count = 2_000_000
    max_context_chars: Count = 256_000
    max_tool_calls: Count = 160


class TaskSnapshot(StrictRecord):
    head_sha: SHA
    base_sha: SHA
    config: StoredConfig = Field(default_factory=StoredConfig)
    prompt_id: Identifier
    source_id: Identifier
    model_id: Identifier


def validate_path(value: str) -> str:
    # Excluded files can have any suffix. This validates location, not tool scope.
    if (not value or len(value) > 512 or value.startswith('/') or '\\' in value or ':' in value
            or any(ord(char) < 32 for char in value)
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('Invalid repository-relative path')
    return value


class StoredEvent(StrictRecord):
    schema_version: Literal[1]
    timestamp: AwareDatetime
    node: str = Field(min_length=1, max_length=64, pattern=r'^[a-z][a-z0-9_]*$')
    path: str | None
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    outcome: Literal['ok', 'error', 'failed', 'skipped', 'excluded']
    termination_reason: Literal['timeout', 'budget_exhausted', 'invalid_output', 'model_error',
        'tool_error', 'insufficient_context', 'invalid_evidence', 'excluded', 'unavailable_diff',
        'review_error', 'completed', 'COMPLETE', 'INCOMPLETE', 'NO_ELIGIBLE_FILES'] | None
    counters: dict[str, Count | None]
    limits: dict[str, Count]
    api_usage: dict[str, Count | None] | None

    @field_validator('path')
    @classmethod
    def valid_path(cls, value):
        return validate_path(value) if value is not None else None

    @field_validator('counters', 'limits', 'api_usage')
    @classmethod
    def known_fields(cls, value, info):
        allowed = {
            'counters': {'attempted', 'reviewed', 'used_chars', 'failures', 'skipped', 'excluded',
                         'model_calls', 'tool_calls', 'context_chars', 'input_chars'},
            'limits': set(StoredConfig.model_fields) - {'engine', 'context_mode', 'model_mode'},
            'api_usage': {'input_tokens', 'output_tokens', 'total_tokens'},
        }[info.field_name]
        if value is not None and value.keys() - allowed:
            raise ValueError('Unexpected resource fields')
        return value

    @classmethod
    def from_event(cls, event: ReviewEvent):
        # JSON validation accepts ISO timestamps while retaining strict counts.
        return cls.model_validate_json(json.dumps(asdict(event), allow_nan=False))
