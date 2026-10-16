from review_pipeline import ReviewBatch


def no_findings_message(batch: ReviewBatch) -> str:
    """Describe an empty findings list without implying unreviewed files passed."""
    if batch.total_files == 0:
        return "No changed files to review."
    if batch.reviewed_files == 0:
        return "No files were successfully reviewed. No conclusion can be drawn."
    if batch.incomplete:
        return (
            "No issues found in the successfully reviewed files. "
            "Review is incomplete."
        )
    return "No issues found in the reviewed changes."


def format_review_batch(batch: ReviewBatch) -> str:
    result = batch.result
    lines = []

    lines.append("## 🤖 Code Review Agent")
    lines.append("")

    lines.append("### Coverage")
    lines.append("")
    status = "INCOMPLETE" if batch.incomplete else "COMPLETE"
    lines.append(f"Status: **{status}**")
    lines.append("")
    lines.append(batch.coverage_summary)
    lines.append("")

    if batch.incomplete:
        lines.append("> Review is incomplete; unreviewed files may contain issues.")
        lines.append("")

    for title, problems in (("Failed files", batch.failures), ("Skipped files", batch.skipped)):
        if problems:
            lines.extend([f"### {title}", ""])
            for problem in problems:
                lines.append(f"- {problem.path}: {problem.reason}")
            lines.append("")

    if result.summary:
        lines.extend(["### Summary", "", result.summary, ""])

    lines.append("### Findings")
    lines.append("")

    if not result.issues:
        lines.append(no_findings_message(batch))
        return "\n".join(lines)

    for issue in result.issues:
        lines.append(
            f"#### {issue.severity.upper()}"
        )
        lines.append("")

        location = issue.file

        if issue.line:
            location += f":{issue.line}"

        lines.append(f"**{location}**")
        lines.append("")

        lines.append(f"**{issue.title}**")
        lines.append("")

        lines.append(issue.description)
        lines.append("")

        if issue.suggestion:
            lines.append(
                f"Suggestion: {issue.suggestion}"
            )
            lines.append("")

    return "\n".join(lines)
