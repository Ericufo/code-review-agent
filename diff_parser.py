from dataclasses import dataclass

@dataclass
class FileDiff:
    path: str
    diff: str

def parse_git_diff(raw_diff: str) -> list[FileDiff]:
    files = []
    current_path = None
    current_lines = []

    for line in raw_diff.splitlines(keepends=True):
        if line.startswith("diff --git "):

            if current_path is not None:
                files.append(
                    FileDiff(
                        path=current_path,
                        diff="".join(current_lines)
                    )
                )

            header = line.rstrip("\r\n")
            marker = " b/"

            if marker not in header:
                raise ValueError(
                    f"Invalid git diff header: {header}"
                )

            current_path = header.rsplit(marker, 1)[1]
            current_lines = [line]

        elif current_path is not None:
            current_lines.append(line)

    if current_path is not None:
        files.append(
            FileDiff(
                path=current_path,
                diff="".join(current_lines)
            )
        )

    return files
