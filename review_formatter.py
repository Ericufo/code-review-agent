from review_models import ReviewResult


def format_review_result(result: ReviewResult) -> str:
    lines = []

    lines.append("## 🤖 Code Review Agent")
    lines.append("")

    lines.append("### Summary")
    lines.append("")
    lines.append(result.summary)
    lines.append("")

    lines.append("### Findings")
    lines.append("")

    if not result.issues:
        lines.append("No issues found.")
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
