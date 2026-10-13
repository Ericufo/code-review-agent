from diff_parser import FileDiff


def github_files_to_diff(files) -> list[FileDiff]:
    result = []

    for file in files:
        patch = file.get("patch")

        if patch is None:
            continue

        filename = file["filename"]

        diff = (
            f"diff --git a/{filename} b/{filename}\n"
            f"{patch}"
        )

        result.append(
            FileDiff(
                path=filename,
                diff=diff,
            )
        )

    return result
