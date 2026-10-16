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


    def test_keep_files_without_a_nonempty_patch(self):
        for payload in ({}, {"patch": None}, {"patch": ""}, {"patch": " \n"}):
            with self.subTest(payload=payload):
                result = github_files_to_diff([{"filename": "image.png", **payload}])

                self.assertEqual(len(result), 1)
                self.assertEqual(result[0].path, "image.png")
                self.assertEqual(result[0].diff, "")
                self.assertIn("GitHub did not provide", result[0].skip_reason)

    def test_mixed_files_keep_original_order_and_count(self):
        files = [
            {"filename": "image.png"},
            {"filename": "main.py", "patch": "@@ -1 +1 @@\n-old\n+new"},
            {"filename": "empty.py", "patch": ""},
        ]

        result = github_files_to_diff(files)

        self.assertEqual([file.path for file in result], [file["filename"] for file in files])
        self.assertIsNone(result[1].skip_reason)


if __name__ == "__main__":
    unittest.main()
