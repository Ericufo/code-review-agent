import subprocess
import os


def run_git_command(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
    )

    return result.stdout

def get_tracked_diff() -> str:
    return run_git_command(["diff", "HEAD"])

def get_untracked_files() -> list[str]:
    output = run_git_command(
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ]
    )

    files = []

    for entry in output.split("\0"):
        if entry.startswith("?? "):
            files.append(entry[3:])

    return files  
    
def get_untracked_file_diff(path: str) -> str:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--no-index",
            "--",
            os.devnull,
            path,
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )

    return result.stdout


def get_git_diff() -> str:
    diff = get_tracked_diff()

    for path in get_untracked_files():
        diff += get_untracked_file_diff(path)

    return diff

if __name__ == "__main__":
    print(get_git_diff())
