import unittest

from review_formatter import format_review_result
from review_models import ReviewResult, ReviewIssue


class TestReviewFormatter(unittest.TestCase):

    def test_empty_issues(self):
        result = ReviewResult(
            summary="No problems found.",
            issues=[]
        )

        output = format_review_result(result)

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
            "No issues found.",
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

        output = format_review_result(result)

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

        output = format_review_result(result)

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


if __name__ == "__main__":
    unittest.main()
