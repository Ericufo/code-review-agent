import subprocess
import os


def run_git_command(
    args: list[str], *, allowed_returncodes: tuple[int, ...] = (0,),
) -> str:
    # Capture bytes: Windows otherwise decodes pipe output in a reader thread
    # using the system code page (often GBK), which can fail on UTF-8 source.
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
    )

    if result.returncode not in allowed_returncodes:
        raise subprocess.CalledProcessError(
            result.returncode, result.args,
            output=result.stdout.decode("utf-8", errors="replace"),
            stderr=result.stderr.decode("utf-8", errors="replace"),
        )

    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Do not silently corrupt source text or filenames with replacement
        # characters. Raise in the main thread so the CLI reports Input error.
        raise RuntimeError(
            "Git output is not valid UTF-8. Local review requires UTF-8 text "
            "files and paths; check the changed files' encoding."
        ) from exc

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
    return run_git_command(
        [
            "diff",
            "--no-index",
            "--",
            os.devnull,
            path,
        ],
        # git diff --no-index returns 1 when it finds differences, not an error.
        allowed_returncodes=(0, 1),
    )


def get_git_diff() -> str:
    diff = get_tracked_diff()

    for path in get_untracked_files():
        diff += get_untracked_file_diff(path)

    return diff

if __name__ == "__main__":
    print(get_git_diff())
