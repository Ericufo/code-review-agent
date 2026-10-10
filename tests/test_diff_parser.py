import unittest

from diff_parser import parse_git_diff


class TestParseGitDiff(unittest.TestCase):

    def test_empty_diff(self):
        result = parse_git_diff("")
        self.assertEqual(result, [])

    def test_multiple_files(self):
        raw_diff = (
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/reviewer.py b/reviewer.py\n"
            "--- a/reviewer.py\n"
            "+++ b/reviewer.py\n"
            "@@ -1 +1 @@\n"
            "-return None\n"
            "+return result\n"
        )

        result = parse_git_diff(raw_diff)

        self.assertEqual(len(result), 2)

        self.assertEqual(result[0].path, "main.py")
        self.assertEqual(result[1].path, "reviewer.py")

        self.assertEqual(
            "".join(file.diff for file in result),
            raw_diff
        )

    def test_single_file(self):
        raw_diff = (
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

        result = parse_git_diff(raw_diff)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].path, "main.py")
        self.assertEqual(result[0].diff, raw_diff)


    def test_no_trailing_newline(self):
        raw_diff = (
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new"
        )

        result = parse_git_diff(raw_diff)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].diff, raw_diff)


    def test_filename_with_spaces(self):
        raw_diff = (
            "diff --git a/my file.py b/my file.py\n"
            "--- a/my file.py\n"
            "+++ b/my file.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

        result = parse_git_diff(raw_diff)

        self.assertEqual(result[0].path, "my file.py")


if __name__ == "__main__":
    unittest.main()
