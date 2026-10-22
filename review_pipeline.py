
from dataclasses import dataclass, field
from collections import Counter
from collections.abc import Callable

from diff_parser import FileDiff
from review_models import ReviewIssue, ReviewResult, SEVERITY_ORDER
from reviewer import review_diff


MAX_FILES = 30
MAX_FILE_CHARS = 20_000
MAX_TOTAL_CHARS = 160_000


def build_review_summary(
    issues: list[ReviewIssue],
    *,
    total_files: int,
    reviewed_files: int,
    incomplete: bool,
    excluded_files: int = 0,
) -> str:
    """Build a short batch overview without another model call or clean-file prose."""
    if total_files == 0:
        return "No changed files to review."
    if excluded_files == total_files:
        return "No eligible files to review. All detected changes are outside the review scope."
    if reviewed_files == 0:
        return "No files were successfully reviewed. No conclusion can be drawn."
    if not issues:
        if incomplete:
            return (
                "No issues found in the successfully reviewed files. "
                "Review is incomplete."
            )
        return "No issues found in the reviewed changes."

    counts = Counter(issue.severity for issue in issues)
    severity_counts = ", ".join(
        f"{counts[level]} {level}" for level in SEVERITY_ORDER if counts[level]
    )
    affected_files = len({issue.file for issue in issues})
    summary = (
        f"Found {len(issues)} potential issue(s) across {affected_files} file(s): "
        f"{severity_counts}."
    )
    if incomplete:
        summary += " Findings cover only successfully reviewed files."
    return summary


@dataclass(frozen=True)
class FileProblem:
    path: str
    reason: str


@dataclass(frozen=True)
class ReviewStep:
    """Small progress event; deliberately excludes source code and model output."""

    node: str
    path: str | None
    attempted: int
    reviewed: int
    failed: int
    skipped: int
    used_chars: int
    model_calls: int = 0
    tool_calls: int = 0
    context_chars: int = 0
    input_chars: int = 0
    model_limit: int = 0
    tool_limit: int = 0
    context_limit: int = 0
    input_limit: int = 0
    excluded: int = 0


@dataclass(frozen=True)
class ReviewBatch:
    result: ReviewResult
    failures: list[FileProblem]
    skipped: list[FileProblem]
    total_files: int
    reviewed_files: int
    excluded: list[FileProblem] = field(default_factory=list)

    @property
    def eligible_files(self) -> int:
        return self.total_files - len(self.excluded)

    @property
    def status(self) -> str:
        if self.incomplete:
            return "INCOMPLETE"
        return "NO_ELIGIBLE_FILES" if self.eligible_files == 0 else "COMPLETE"

    @property
    def incomplete(self) -> bool:
        return bool(self.failures or self.skipped)

    @property
    def coverage_summary(self) -> str:
        return (
            f"Reviewed {self.reviewed_files}/{self.eligible_files} file(s). "
            f"Found {len(self.result.issues)} issue(s). "
            f"Failed: {len(self.failures)}; skipped: {len(self.skipped)}."
            + (f" Excluded: {len(self.excluded)}; detected: {self.total_files}." if self.excluded else "")
        )


def get_skip_reason(
    file_diff: FileDiff,
    *,
    attempted: int,
    used_chars: int,
    max_files: int,
    max_file_chars: int,
    max_total_chars: int,
) -> str | None:
    """Shared eligibility rules for the classic loop and the graph."""
    if file_diff.skip_reason is not None:
        return file_diff.skip_reason
    diff = file_diff.diff
    if "\nGIT binary patch\n" in f"\n{diff}\n" or any(
        line.startswith("Binary files ") for line in diff.splitlines()
    ):
        return "binary diff is not supported"
    if len(diff) > max_file_chars:
        return f"diff exceeds {max_file_chars} characters"
    if attempted >= max_files:
        return f"maximum {max_files} file attempts reached"
    if used_chars + len(diff) > max_total_chars:
        return f"total diff budget {max_total_chars} characters exceeded"
    return None


def review_file_issues(
    file_diff: FileDiff,
    review_fn: Callable[[str], ReviewResult],
    *, verified_context: bool = False,
) -> list[ReviewIssue]:
    file_result = review_fn(file_diff.diff)
    # Revalidate after assigning the authoritative Git path.
    return [
        ReviewIssue.model_validate({**issue.model_dump(), "file": file_diff.path,
                                   "evidence": issue.evidence if verified_context else []})
        for issue in file_result.issues
    ]


def finish_review_batch(
    *,
    issues: list[ReviewIssue],
    failures: list[FileProblem],
    skipped: list[FileProblem],
    total_files: int,
    reviewed_files: int,
    excluded: list[FileProblem] | None = None,
) -> ReviewBatch:
    # Stable sorting preserves the model's ordering within each severity level.
    ordered = sorted(issues, key=lambda issue: SEVERITY_ORDER.index(issue.severity))
    return ReviewBatch(
        result=ReviewResult(
            summary=build_review_summary(
                ordered, total_files=total_files, reviewed_files=reviewed_files,
                incomplete=bool(failures or skipped),
                excluded_files=len(excluded or []),
            ),
            issues=ordered,
        ),
        failures=failures,
        skipped=skipped,
        total_files=total_files,
        reviewed_files=reviewed_files,
        excluded=excluded or [],
    )


def review_files(
    file_diffs: list[FileDiff],
    *,
    max_files: int = MAX_FILES,
    max_file_chars: int = MAX_FILE_CHARS,
    max_total_chars: int = MAX_TOTAL_CHARS,
    engine: str = "classic",
    on_step: Callable[[ReviewStep], None] | None = None,
    context=None,
    context_request_fn=None,
    max_model_calls: int | None = None,
    max_input_chars: int | None = None,
    max_context_chars: int | None = None,
    max_tool_calls: int | None = None,
) -> ReviewBatch:
    """Review text diffs independently and keep partial results on failure."""

    if engine not in ("classic", "langgraph"):
        raise ValueError(f"Unknown review engine: {engine}")
    if any(limit < 0 for limit in (max_files, max_file_chars, max_total_chars)):
        raise ValueError("Review budgets must be non-negative")
    if context is not None and engine != "langgraph":
        raise ValueError("Repository context requires the langgraph engine")
    overrides = (max_model_calls, max_input_chars, max_context_chars, max_tool_calls)
    if any(value is not None for value in overrides):
        if any(value is not None and (type(value) is not int or value < 0) for value in overrides):
            raise ValueError("Context budgets must be non-negative integers")
        if context is None:
            raise ValueError("Context budget override requires repository context")
    if engine == "langgraph":
        # Lazy import keeps the V3 baseline usable without LangGraph installed.
        from review_graph import review_files_graph

        return review_files_graph(
            file_diffs, review_fn=review_diff, max_files=max_files,
            max_file_chars=max_file_chars, max_total_chars=max_total_chars,
            on_step=on_step,
            context=context, context_request_fn=context_request_fn,
            max_model_calls=max_model_calls,
            max_input_chars=max_input_chars, max_context_chars=max_context_chars,
            max_tool_calls=max_tool_calls,
        )
    if on_step is not None:
        raise ValueError("Step tracing requires the langgraph engine")

    issues = []
    failures: list[FileProblem] = []
    skipped: list[FileProblem] = []

    attempted = 0
    used_chars = 0
    reviewed = 0

    for file_diff in file_diffs:
        path, diff = file_diff.path, file_diff.diff

        reason = get_skip_reason(
            file_diff, attempted=attempted, used_chars=used_chars,
            max_files=max_files, max_file_chars=max_file_chars,
            max_total_chars=max_total_chars,
        )
        if reason is not None:
            skipped.append(FileProblem(path, reason))
            continue

        attempted += 1
        used_chars += len(diff)

        try:
            validated_issues = review_file_issues(file_diff, review_diff)

        except Exception as exc:
            failures.append(
                FileProblem(
                    path,
                    f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        reviewed += 1
        issues.extend(validated_issues)

    return finish_review_batch(
        issues=issues, failures=failures, skipped=skipped,
        total_files=len(file_diffs), reviewed_files=reviewed,
    )
