from diff_parser import FileDiff


def github_files_to_diff(files) -> list[FileDiff]:
    result = []

    for file in files:
        filename = file["filename"]
        patch = file.get("patch")

        if patch is None or not patch.strip():
            result.append(FileDiff(
                path=filename,
                diff="",
                skip_reason="GitHub did not provide a non-empty text patch",
            ))
            continue

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
