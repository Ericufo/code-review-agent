"""Shared application service for CLI and future HTTP task execution.

Imports perform no environment loading, I/O, model calls or worker startup.
Callers own credentials, presentation, publication and execution scheduling.
"""
from collections.abc import Callable
from dataclasses import dataclass
import json
import subprocess

from diff_parser import FileDiff, parse_git_diff
from git_utils import get_git_diff
from github_diff import github_files_to_diff
from github_review import PullRequestRevision, ensure_current_revision, get_review_revision
from repository_context import RepositoryContext, GitHubRepositoryContext, capture_local_context
from review_checkpoint import PersistenceGuard, ReviewSink
from review_pipeline import MAX_FILES, MAX_FILE_CHARS, MAX_TOTAL_CHARS, ReviewBatch, review_files
from review_tools import ContextBudget


CONTEXT_BUDGET_OPTIONS = ('max_model_calls', 'max_input_chars', 'max_context_chars', 'max_tool_calls')


@dataclass(frozen=True)
class ReviewConfig:
    engine: str = 'langgraph'
    context_mode: str = 'diff'
    max_files: int = MAX_FILES
    max_file_chars: int = MAX_FILE_CHARS
    max_total_chars: int = MAX_TOTAL_CHARS
    max_model_calls: int | None = None
    max_input_chars: int | None = None
    max_context_chars: int | None = None
    max_tool_calls: int | None = None

    def __post_init__(self):
        if self.engine not in ('classic', 'langgraph') or self.context_mode not in ('diff', 'repo'):
            raise ValueError('Unknown review engine or context mode')
        if self.context_mode == 'repo' and self.engine != 'langgraph':
            raise ValueError('Repository context requires the langgraph engine')
        for name in ('max_files', 'max_file_chars', 'max_total_chars', *CONTEXT_BUDGET_OPTIONS):
            value = getattr(self, name)
            if value is None and name in CONTEXT_BUDGET_OPTIONS:
                continue
            if type(value) is not int or value < 0:
                raise ValueError('Review budgets must be non-negative integers')
        if self.context_mode != 'repo' and self.context_overrides():
            raise ValueError('Context budget override requires repository context')

    def context_overrides(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in CONTEXT_BUDGET_OPTIONS
                if getattr(self, name) is not None}

    def context_limits(self) -> ContextBudget:
        """Fresh counters for each execution/display; configuration is immutable."""
        return ContextBudget(**self.context_overrides())


@dataclass(frozen=True)
class PreparedReview:
    files: tuple[FileDiff, ...]
    config: ReviewConfig
    context: RepositoryContext | None = None
    revision: PullRequestRevision | None = None
    detected_files: int = 0
    validate_after: Callable[[], None] | None = None


def get_local_files() -> list[FileDiff]:
    try:
        diff = get_git_diff()
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() if exc.stderr else str(exc)
        raise RuntimeError(f'Git error: {message}') from exc
    return parse_git_diff(diff) if diff.strip() else []


def get_github_files(client, pr_number: int) -> list[FileDiff]:
    return github_files_to_diff(client.get_pull_request_files(number=pr_number))


def select_local_files(files: list[FileDiff], paths: list[str] | None) -> list[FileDiff]:
    if paths is None:
        return files
    requested = set(paths)
    missing = requested - {item.path for item in files}
    if missing:
        raise ValueError('Selected paths must have current Git changes: ' + json.dumps(sorted(missing)))
    return [item for item in files if item.path in requested]


def prepare_local_review(config: ReviewConfig, *, paths: list[str] | None = None,
                         file_loader=None, context_factory=None) -> PreparedReview:
    load = file_loader or get_local_files
    context = (context_factory or capture_local_context)() if config.context_mode == 'repo' else None
    all_files = load()
    selected_paths = list(paths) if paths is not None else None
    files = tuple(select_local_files(all_files, selected_paths))
    if context is not None:
        context.ensure_unchanged()

    def validate_after():
        if context is not None:
            context.ensure_unchanged()
            if tuple(select_local_files(load(), selected_paths)) != files:
                raise RuntimeError('Local diff changed during review; rerun required')

    return PreparedReview(files, config, context, detected_files=len(all_files),
                          validate_after=validate_after)


def prepare_github_review(client, pr_number: int, config: ReviewConfig, *,
                          expected_head_sha: str | None = None,
                          file_loader=None, context_factory=None) -> PreparedReview:
    """Capture the current PR revision and reject mixed/stale file listings.

    The PR-files endpoint is mutable: check its revision after fetching, as the
    CLI did. Future queued tasks need an immutable diff provider for their saved
    revision; this helper must not be used to silently refresh a queued task.
    """
    revision = get_review_revision(client, pr_number, expected_head_sha=expected_head_sha)
    files = tuple((file_loader or get_github_files)(client, pr_number))
    ensure_current_revision(client, pr_number, revision)
    context = None
    if config.context_mode == 'repo' and files:
        context = (context_factory or GitHubRepositoryContext)(client, revision)
    return PreparedReview(files, config, context, revision, len(files))


def execute_review(prepared: PreparedReview, *, review_fn=None, context_request_fn=None,
                   sink: ReviewSink | None = None, on_step=None, on_record=None,
                   before_work: Callable[[], None] | None = None) -> ReviewBatch:
    """Run validated review logic with injectable model/context/persistence.

    No publication or CLI output. Ordinary observers remain best-effort;
    persistence failures abort the run and prevent finalization.
    """
    config, context = prepared.config, prepared.context
    if config.context_mode == 'repo' and prepared.files and context is None:
        raise ValueError('Repository context is required for nonempty repo reviews')
    if config.context_mode == 'diff' and context is not None:
        raise ValueError('Diff-only reviews cannot use repository context')
    if prepared.revision is not None and context is not None:
        expected = {'head': prepared.revision.head_sha, 'base': prepared.revision.base_sha}
        if context.revisions != expected:
            raise ValueError('Repository context does not match the prepared PR revision')
    persistence = PersistenceGuard(sink, before_work=before_work)
    batch = review_files(list(prepared.files), engine=config.engine, context=context,
        max_files=config.max_files, max_file_chars=config.max_file_chars,
        max_total_chars=config.max_total_chars,
        **(config.context_overrides() if context is not None else {}),
        review_fn=review_fn, context_request_fn=context_request_fn,
        on_step=on_step, on_record=on_record, persistence=persistence)
    persistence.check()
    if prepared.validate_after is not None:
        prepared.validate_after()
    persistence.finish(batch)
    return batch
