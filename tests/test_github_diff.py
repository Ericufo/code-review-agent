import unittest

from github_diff import github_files_to_diff


class TestGithubDiff(unittest.TestCase):

    def test_github_files_to_diff(self):

        files = [
            {
                "filename": "main.py",
                "patch": (
                    "@@ -1 +1 @@\n"
                    "-old\n"
                    "+new"
                ),
            }
        ]

        result = github_files_to_diff(files)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].path, "main.py")
        self.assertIn("old", result[0].diff)


    def test_skip_file_without_patch(self):

        files = [
            {
                "filename": "image.png",
                "patch": None,
            }
        ]

        result = github_files_to_diff(files)

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
