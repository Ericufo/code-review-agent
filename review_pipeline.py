
from dataclasses import dataclass

from diff_parser import FileDiff
from review_models import ReviewResult
from reviewer import review_diff


MAX_FILES = 30
MAX_FILE_CHARS = 20_000
MAX_TOTAL_CHARS = 80_000


@dataclass(frozen=True)
class FileProblem:
    path: str
    reason: str


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


def review_files(
    file_diffs: list[FileDiff],
    *,
    max_files: int = MAX_FILES,
    max_file_chars: int = MAX_FILE_CHARS,
    max_total_chars: int = MAX_TOTAL_CHARS,
) -> ReviewBatch:
    """Review text diffs independently and keep partial results on failure."""

    if not file_diffs:
        return ReviewBatch(
            result=ReviewResult(
                summary="No changed files to review.",
                issues=[],
            ),
            failures=[],
            skipped=[],
            total_files=0,
            reviewed_files=0,
        )

    summaries: list[str] = []
    issues = []
    failures: list[FileProblem] = []
    skipped: list[FileProblem] = []

    attempted = 0
    used_chars = 0
    reviewed = 0

    for file_diff in file_diffs:
        path, diff = file_diff.path, file_diff.diff

        # Keep unavailable patches in the denominator without using LLM budget.
        if file_diff.skip_reason is not None:
            skipped.append(FileProblem(path, file_diff.skip_reason))
            continue

        # Skip binary patches that cannot be reviewed as source text.
        if "\nGIT binary patch\n" in f"\n{diff}\n" or any(
            line.startswith("Binary files ")
            for line in diff.splitlines()
        ):
            skipped.append(
                FileProblem(path, "binary diff is not supported")
            )
            continue

        # Protect against oversized requests.
        if len(diff) > max_file_chars:
            skipped.append(
                FileProblem(
                    path,
                    f"diff exceeds {max_file_chars} characters",
                )
            )
            continue

        if attempted >= max_files:
            skipped.append(
                FileProblem(
                    path,
                    f"maximum {max_files} LLM calls reached",
                )
            )
            continue

        if used_chars + len(diff) > max_total_chars:
            skipped.append(
                FileProblem(
                    path,
                    f"total diff budget {max_total_chars} characters exceeded",
                )
            )
            continue

        attempted += 1
        used_chars += len(diff)

        try:
            file_result = review_diff(diff)

            # Revalidate after assigning the authoritative Git path.
            validated_issues = [
                type(issue).model_validate({
                    **issue.model_dump(),
                    "file": path,
                })
                for issue in file_result.issues
            ]

        except Exception as exc:
            failures.append(
                FileProblem(
                    path,
                    f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        reviewed += 1
        summaries.append(
            f"{path}: {file_result.summary.strip()}"
        )
        issues.extend(validated_issues)

    return ReviewBatch(
        result=ReviewResult(
            summary="\n".join(summaries),
            issues=issues,
        ),
        failures=failures,
        skipped=skipped,
        total_files=len(file_diffs),
        reviewed_files=reviewed,
    )
