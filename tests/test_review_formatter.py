import unittest

from review_formatter import format_review_batch
from review_models import ReviewResult, ReviewIssue
from review_pipeline import FileProblem, ReviewBatch


class TestReviewFormatter(unittest.TestCase):
    
    def format_complete_result(self, result):
        return format_review_batch(ReviewBatch(
            result=result, failures=[], skipped=[], total_files=1, reviewed_files=1,
        ))

    def test_empty_issues(self):
        result = ReviewResult(
            summary="No problems found.",
            issues=[]
        )

        output = self.format_complete_result(result)

        self.assertIn(
            "## 🤖 Code Review Agent",
            output
        )
        self.assertIn(
            "### Summary",
            output
        )
        self.assertIn(
            "No problems found.",
            output
        )
        self.assertIn(
            "No issues found in the reviewed changes.",
            output
        )


    def test_single_issue_with_line_and_suggestion(self):
        result = ReviewResult(
            summary="Found potential issue.",
            issues=[
                ReviewIssue(
                    severity="high",
                    file="main.py",
                    line=42,
                    title="Missing error handling",
                    description="The API call may fail without exception handling.",
                    suggestion="Add try-except around the request."
                )
            ]
        )

        output = self.format_complete_result(result)

        self.assertIn(
            "HIGH",
            output
        )
        self.assertIn(
            "main.py:42",
            output
        )
        self.assertIn(
            "Missing error handling",
            output
        )
        self.assertIn(
            "The API call may fail without exception handling.",
            output
        )
        self.assertIn(
            "Suggestion: Add try-except around the request.",
            output
        )


    def test_issue_without_line_and_suggestion(self):
        result = ReviewResult(
            summary="Found style issue.",
            issues=[
                ReviewIssue(
                    severity="low",
                    file="utils.py",
                    title="Naming could be improved",
                    description="The variable name is not descriptive.",
                )
            ]
        )

        output = self.format_complete_result(result)

        self.assertIn(
            "LOW",
            output
        )
        self.assertIn(
            "utils.py",
            output
        )
        self.assertIn(
            "Naming could be improved",
            output
        )
        self.assertIn(
            "The variable name is not descriptive.",
            output
        )

        self.assertNotIn(
            "None",
            output
        )

    def test_partial_review_shows_coverage_and_all_reasons(self):
        output = format_review_batch(ReviewBatch(
            result=ReviewResult(summary="good.py: ok", issues=[]),
            failures=[FileProblem("failed.py", "TimeoutError: model timeout")],
            skipped=[FileProblem("image.png", "patch unavailable")],
            total_files=3, reviewed_files=1,
        ))

        self.assertIn("Status: **INCOMPLETE**", output)
        self.assertIn("Reviewed 1/3 file(s)", output)
        self.assertIn("Failed: 1; skipped: 1", output)
        self.assertIn("failed.py: TimeoutError: model timeout", output)
        self.assertIn("image.png: patch unavailable", output)
        self.assertIn("No issues found in the successfully reviewed files", output)
        self.assertNotIn("No issues found.", output)

    def test_zero_successful_reviews_never_claims_no_issues(self):
        for failures, skipped in (
            ([FileProblem("failed.py", "timeout")], []),
            ([], [FileProblem("image.png", "patch unavailable")]),
        ):
            with self.subTest(failures=failures, skipped=skipped):
                output = format_review_batch(ReviewBatch(
                    result=ReviewResult(summary="", issues=[]),
                    failures=failures, skipped=skipped,
                    total_files=1, reviewed_files=0,
                ))

                self.assertIn("Status: **INCOMPLETE**", output)
                self.assertIn("No files were successfully reviewed", output)
                self.assertIn("No conclusion can be drawn", output)
                self.assertNotIn("No issues found", output)

    def test_partial_review_keeps_findings_and_warning(self):
        output = format_review_batch(ReviewBatch(
            result=ReviewResult(summary="found a bug", issues=[ReviewIssue(
                severity="high", file="main.py", line=5,
                title="Incorrect return value", description="Returns the old value.",
            )]),
            failures=[], skipped=[FileProblem("image.png", "patch unavailable")],
            total_files=2, reviewed_files=1,
        ))

        self.assertIn("Review is incomplete", output)
        self.assertIn("Found 1 issue(s)", output)
        self.assertIn("Incorrect return value", output)
        self.assertIn("main.py:5", output)
        self.assertIn("image.png: patch unavailable", output)

    def test_empty_input_is_not_reported_as_a_clean_review(self):
        output = format_review_batch(ReviewBatch(
            result=ReviewResult(summary="", issues=[]),
            failures=[], skipped=[], total_files=0, reviewed_files=0,
        ))

        self.assertIn("No changed files to review", output)
        self.assertNotIn("No issues found", output)


if __name__ == "__main__":
    unittest.main()
