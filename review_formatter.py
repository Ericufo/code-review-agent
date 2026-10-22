from review_models import SEVERITY_ORDER
from review_pipeline import ReviewBatch, build_review_summary


def format_review_batch(batch: ReviewBatch) -> str:
    result = batch.result
    lines = []

    lines.append("## 🤖 Code Review Agent")
    lines.append("")

    lines.append("### Summary")
    lines.append("")
    status = batch.status
    lines.append(f"Status: **{status}**")
    lines.append("")
    lines.append(batch.coverage_summary)
    lines.append("")
    # Derive the overview from structured data even if a caller supplies an old
    # per-file summary. Do not repeat or expose clean-file model commentary.
    lines.append(build_review_summary(
        result.issues, total_files=batch.total_files, reviewed_files=batch.reviewed_files,
        incomplete=batch.incomplete,
        excluded_files=len(batch.excluded),
    ))
    lines.append("")

    if batch.incomplete:
        lines.append("> Review is incomplete; unreviewed files may contain issues.")
        lines.append("")

    if result.issues:
        lines.extend(["### Findings", ""])

    issues = sorted(result.issues, key=lambda issue: SEVERITY_ORDER.index(issue.severity))
    for index, issue in enumerate(issues, start=1):
        lines.append(f"#### {index}. [{issue.severity.upper()}] {issue.title}")
        lines.append("")

        location = issue.file

        if issue.line:
            location += f":{issue.line}"

        lines.append(f"**{location}**")
        lines.append("")

        lines.append(issue.description)
        lines.append("")

        if issue.suggestion:
            lines.append(
                f"Suggestion: {issue.suggestion}"
            )
            lines.append("")

        for evidence in issue.evidence:
            lines.append(
                f"Evidence: `{evidence.repository}@{evidence.revision}` "
                f"`{evidence.path}:{evidence.start_line}-{evidence.end_line}`"
            )
        if issue.evidence:
            lines.append("")

    for title, problems in (("Failed files", batch.failures), ("Skipped files", batch.skipped),
                            ("Excluded files (outside review scope)", batch.excluded)):
        if problems:
            lines.extend([f"### {title}", ""])
            for problem in problems:
                lines.append(f"- {problem.path}: {problem.reason}")
            lines.append("")

    return "\n".join(lines).rstrip()
