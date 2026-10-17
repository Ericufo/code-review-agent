
from dataclasses import dataclass
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
) -> str:
    """Build a short batch overview without another model call or clean-file prose."""
    if total_files == 0:
        return "No changed files to review."
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


@dataclass(frozen=True)
class ReviewBatch:
    result: ReviewResult
    failures: list[FileProblem]
    skipped: list[FileProblem]
    total_files: int
    reviewed_files: int

    @property
    def incomplete(self) -> bool:
        return bool(self.failures or self.skipped)

    @property
    def coverage_summary(self) -> str:
        return (
            f"Reviewed {self.reviewed_files}/{self.total_files} file(s). "
            f"Found {len(self.result.issues)} issue(s). "
            f"Failed: {len(self.failures)}; skipped: {len(self.skipped)}."
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
        return f"maximum {max_files} LLM calls reached"
    if used_chars + len(diff) > max_total_chars:
        return f"total diff budget {max_total_chars} characters exceeded"
    return None


def review_file_issues(
    file_diff: FileDiff,
    review_fn: Callable[[str], ReviewResult],
) -> list[ReviewIssue]:
    file_result = review_fn(file_diff.diff)
    # Revalidate after assigning the authoritative Git path.
    return [
        ReviewIssue.model_validate({**issue.model_dump(), "file": file_diff.path})
        for issue in file_result.issues
    ]


def finish_review_batch(
    *,
    issues: list[ReviewIssue],
    failures: list[FileProblem],
    skipped: list[FileProblem],
    total_files: int,
    reviewed_files: int,
) -> ReviewBatch:
    # Stable sorting preserves the model's ordering within each severity level.
    ordered = sorted(issues, key=lambda issue: SEVERITY_ORDER.index(issue.severity))
    return ReviewBatch(
        result=ReviewResult(
            summary=build_review_summary(
                ordered, total_files=total_files, reviewed_files=reviewed_files,
                incomplete=bool(failures or skipped),
            ),
            issues=ordered,
        ),
        failures=failures,
        skipped=skipped,
        total_files=total_files,
        reviewed_files=reviewed_files,
    )


def review_files(
    file_diffs: list[FileDiff],
    *,
    max_files: int = MAX_FILES,
    max_file_chars: int = MAX_FILE_CHARS,
    max_total_chars: int = MAX_TOTAL_CHARS,
    engine: str = "classic",
    on_step: Callable[[ReviewStep], None] | None = None,
) -> ReviewBatch:
    """Review text diffs independently and keep partial results on failure."""

    if engine not in ("classic", "langgraph"):
        raise ValueError(f"Unknown review engine: {engine}")
    if any(limit < 0 for limit in (max_files, max_file_chars, max_total_chars)):
        raise ValueError("Review budgets must be non-negative")
    if engine == "langgraph":
        # Lazy import keeps the V3 baseline usable without LangGraph installed.
        from review_graph import review_files_graph

        return review_files_graph(
            file_diffs, review_fn=review_diff, max_files=max_files,
            max_file_chars=max_file_chars, max_total_chars=max_total_chars,
            on_step=on_step,
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
