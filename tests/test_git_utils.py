import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_utils import (
    get_git_diff,
    get_untracked_file_diff,
    get_untracked_files,
    run_git_command,
)


class TestGitOutputDecoding(unittest.TestCase):
    def test_utf8_pipe_output_is_independent_of_the_windows_code_page(self):
        payload = "+中文学习说明：状态图与预算\n".encode("utf-8")
        real_run = subprocess.run

        def emit_utf8(_args, **kwargs):
            # Use a real child process and its pipes, forcing the legacy default
            # to GBK if text mode is requested. The old code fails this case.
            if kwargs.get("text"):
                kwargs.setdefault("encoding", "gbk")
            return real_run([
                sys.executable, "-c",
                f"import sys; sys.stdout.buffer.write({payload!r})",
            ], **kwargs)

        with patch("git_utils.subprocess.run", side_effect=emit_utf8):
            self.assertEqual(run_git_command(["diff", "HEAD"]), payload.decode("utf-8"))

    @patch("git_utils.subprocess.run")
    def test_nul_separated_unicode_paths_are_decoded_without_replacement(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["git"], 0, "?? docs/学习说明.md\0?? spaced name.py\0".encode("utf-8"), b"",
        )
        self.assertEqual(get_untracked_files(), ["docs/学习说明.md", "spaced name.py"])

    def test_real_git_diff_of_untracked_utf8_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.md"
            path.write_text("中文说明：工具调用与审查。\n", encoding="utf-8")
            diff = get_untracked_file_diff(str(path))
        self.assertIn("+中文说明：工具调用与审查。", diff)

    @patch("git_utils.subprocess.run")
    def test_tracked_and_untracked_utf8_diffs_can_be_combined(self, run):
        run.side_effect = [
            subprocess.CompletedProcess(["git"], 0, "+已有修改\n".encode("utf-8"), b""),
            subprocess.CompletedProcess(["git"], 0, b"?? docs/notes.md\0", b""),
            subprocess.CompletedProcess(["git"], 1, "+新增说明\n".encode("utf-8"), b""),
        ]
        self.assertEqual(get_git_diff(), "+已有修改\n+新增说明\n")

    @patch("git_utils.subprocess.run")
    def test_invalid_utf8_is_an_explicit_input_failure(self, run):
        run.return_value = subprocess.CompletedProcess(["git"], 0, b"+bad\xff\n", b"")
        with self.assertRaisesRegex(RuntimeError, "Git output is not valid UTF-8"):
            run_git_command(["diff", "HEAD"])

    @patch("git_utils.subprocess.run")
    def test_git_errors_preserve_code_and_readable_stderr(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["git", "diff"], 128, b"", "fatal: 无法读取\n".encode("utf-8"),
        )
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            run_git_command(["diff"])
        self.assertEqual(caught.exception.returncode, 128)
        self.assertEqual(caught.exception.stderr, "fatal: 无法读取\n")

    @patch("git_utils.subprocess.run")
    def test_non_utf8_diagnostic_does_not_hide_git_failure(self, run):
        run.return_value = subprocess.CompletedProcess(["git"], 2, b"", b"fatal: \xff")
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            get_untracked_file_diff("missing.py")
        self.assertEqual(caught.exception.returncode, 2)
        self.assertIn("fatal:", caught.exception.stderr)

    @patch("git_utils.subprocess.run")
    def test_exit_one_is_accepted_only_for_no_index_diff(self, run):
        run.return_value = subprocess.CompletedProcess(["git"], 1, b"diff", b"")
        self.assertEqual(get_untracked_file_diff("new.py"), "diff")
        with self.assertRaises(subprocess.CalledProcessError):
            run_git_command(["status"])


if __name__ == "__main__":
    unittest.main()
