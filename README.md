# Code Review Agent

A Python tool that reviews local Git changes and GitHub pull requests with an
LLM. V4.1 adds LangGraph orchestration and node progress tracing on top of V3's
automatic PR comments, explicit review coverage, and checks for obsolete results.
The model still reviews diffs only; repository tools and context are planned next.

## Features

- Reviews tracked and untracked local changes, or the changed files of a PR
- Validates structured findings with Pydantic
- Preserves successful results when other files fail
- Reports reviewed, failed, and skipped file counts with reasons
- Paginates GitHub file/comment lists and rejects incomplete file retrieval
- Creates a marked PR summary comment and updates it on subsequent runs
- Records the reviewed head and base commits in the comment
- Cancels superseded Actions runs and checks revisions before publishing
- Includes mocked tests that need no API keys or network access
- Runs a bounded LangGraph workflow, with a classic V3 engine for comparison
- Shows node transitions and budget counters with `--trace`

## Setup and manual use

Python 3.10+ and Git are required. Actions uses Python 3.11.
On Windows with Git Bash:

```bash
python -m venv .venv
source .venv/Scripts/activate
python -m pip install -r requirements.txt
```

On Linux/macOS, use `source .venv/bin/activate` instead. Without activation,
Windows Git Bash can run the project interpreter as `./.venv/Scripts/python.exe`.

Mocked tests need no `.env` or API keys. For live
manual reviews, supply environment variables or create a local `.env`:

```bash
# Create only if absent, preserving any existing credentials
test -e .env || cp .env.example .env
```

Fill in the needed values below. `main.py` loads `.env` automatically, and
`.gitignore` excludes it; commit only the empty `.env.example` template.
Local Git reviews require only `DEEPSEEK_API_KEY`; manual GitHub PR reviews also
require the three GitHub variables. Actions uses its configured secrets and
workflow environment, without a local `.env` file.

| Variable | Purpose |
| --- | --- |
| `DEEPSEEK_API_KEY` | Model access for live reviews |
| `GITHUB_TOKEN` | GitHub access for manual PR reviews |
| `GITHUB_OWNER` | Target repository owner |
| `GITHUB_REPO` | Target repository name, without the owner prefix |

The model configuration is in `reviewer.py`. Diff contents are sent to the
external model API.

```bash
# Review local changes
python main.py

# Review an open, non-draft PR and publish its summary
python main.py --github-pr 7

# Show LangGraph progress on stderr (local review)
python main.py --trace

# Use the original loop for comparison
python main.py --engine classic

# Test the graph with simulated results; no model or GitHub calls
python -m unittest discover -s tests -p test_review_graph.py -v
```

The CLI defaults to `--engine langgraph`. The Python `review_files()` function
keeps its `classic` default for compatibility; pass `engine="langgraph"` to opt in.
`--trace` requires the LangGraph engine. It prints filenames and counters, not
diffs or model responses, and is not included in PR comments. Install updated
dependencies with `python -m pip install -r requirements.txt` when upgrading V3.

Comments start with the hidden marker `<!-- code-review-agent -->` on its own
line. Subsequent runs update only a comment with that first line and the expected
author; quoted markers and other authors' comments are preserved. Manual runs
resolve the token's user through GitHub's `/user` endpoint. The Actions workflow
explicitly sets `GITHUB_COMMENT_AUTHOR=github-actions[bot]`, because its
installation token cannot use `/user`. A custom installation-token setup must
likewise configure the expected publishing login. Switching between a personal
token and the Actions bot can create separate comments; neither adopts the
other's comment. If the PR has no changed files, the publisher updates its
comment to say so instead of retaining old findings.

Reports contain a short overall summary computed from the findings, followed by
issues ordered by severity. Clean-file summaries are omitted. Failure and skip
reasons remain visible even when no issues were found. Summary generation does
not add a model call. The prompt requests concise, actionable findings, but
these constraints do not guarantee that all model findings are correct.

## Automatic PR reviews

| Workflow | Trigger | Behavior |
| --- | --- | --- |
| `Python Tests` (`tests.yml`) | Push and PR events | Runs mocked tests without model credentials |
| `PR Review` (`review.yml`) | PR opened, synchronized, reopened, or marked ready for review | Calls the model and creates or updates the summary |

Enable live reviews:

1. In **Settings → Secrets and variables → Actions**, create a repository secret
   named `DEEPSEEK_API_KEY`.
2. Ensure Actions is enabled and repository/organization policies allow the
   requested permissions: `contents: read` and `pull-requests: write`.
3. Push the changes to a branch with an open PR, or open a non-draft PR from a
   branch in this repository. Logs appear under **Actions → PR Review**.

GitHub supplies `GITHUB_TOKEN` automatically. A personal token, owner secret, and
repository-name secret are not needed in Actions; repository information comes
from the event. A missing model secret fails early with a configuration error.

Only non-draft, same-repository PRs are automatically reviewed. Fork PRs and
Dependabot PRs are skipped by `PR Review`; the ordinary test workflow still runs.
The review job executes PR repository code with credentials, so this scope
assumes collaborators who can push branches are trusted. The event is
`pull_request`; it does not use privileged `pull_request_target` execution.

### Results, failures, and retries

- A complete review exits with code `0`, even when it finds issues. Findings are
  advisory; this version does not block based on severity.
- A failed or skipped file makes coverage **INCOMPLETE** and exits with code `1`.
  Successful findings and failure/skip reasons are still published when possible.
- GitHub retrieval or publication failures also exit with code `1`.
- Closed/draft PRs and superseded runs are explicitly skipped with code `0` and
  do not publish. A successful check alone does not prove a review ran; check the
  log and the comment's commit SHA.
- Each review job has a 20-minute timeout. A cancelled or timed-out job may leave
  the previous comment in place; its recorded SHA identifies its age.

To retry after fixing configuration or a transient failure, use **Re-run jobs**
on the run for the current commit. Re-running a job for an older head is skipped.

### Superseded reviews

Each PR has an Actions concurrency group. A new event cancels the previous run.
The CLI also:

1. Checks the event's expected head SHA before fetching files.
2. Records head and base SHA, then checks them after file retrieval.
3. Checks both SHAs and PR state again after fetching comments, immediately
   before creating or updating the summary.

Actions invokes:

```bash
python main.py --github-pr "$PR_NUMBER" --expected-head-sha "$EXPECTED_HEAD_SHA"
```

Manual reviews also check revisions, starting from the current head rather than
an event's expected head. Base-only updates and retargeting are not automatic
triggers in this version; re-run the current job when needed.

These checks reduce stale writes but are not atomic: the PR can change between
the final check and the comment request. The comment's head/base SHA identifies
the reviewed revision. Independent manual invocations are not serialized by
Actions concurrency.

## Architecture

```text
Local CLI                         GitHub Actions / PR CLI
    |                                        |
git_utils.py                      github_client.py + github_review.py
    |                              fetch PR, record/check revision
diff_parser.py                              |
    |                                  github_diff.py
    +-------------------+--------------------+
                        |
                    FileDiff[]
                        |
                review_pipeline.py
              shared budgets / validation
                        |
         review_graph.py (default CLI) or classic loop
          check -> review / skip -> check -> summarize
                        |
                    reviewer.py
                   model request
                        |
                review_models.py
                Pydantic validation
                        |
                    ReviewBatch
          findings + coverage + failures/skips
                        |
             +----------+----------------+
             |                           |
         CLI output              review_formatter.py
                                         |
                                 github_comment.py
                               final revision check
                               create/update summary
```

`FileDiff.skip_reason` keeps files with unavailable patches in coverage counts
without calling the model. `ReviewBatch` carries findings and coverage, so an
empty issue list is not mistaken for a complete review when files failed or were
skipped. `PullRequestRevision` identifies the head/base pair being reviewed.

## Validation

```bash
python -m unittest discover -s tests -v
```

If actionlint is installed:

```bash
actionlint .github/workflows/tests.yml .github/workflows/review.yml
```

After configuring the secret and pushing, verify the live workflow with a small PR:

1. Open a non-draft, same-repository PR. Confirm both workflows run and a summary
   appears with the current head SHA.
2. Push another commit. Confirm the same comment is updated and its SHA changes.
   Re-running the current job should also update that comment.
3. Push twice while a review is running. Confirm the older run is cancelled or
   reports that it was superseded, and the final summary identifies the new SHA.
4. Include an unavailable or oversized patch. Confirm the summary reports
   **INCOMPLETE** and the review job fails rather than claiming all files passed.

Mocked tests do not validate a real API key, Actions policies, or live model
behavior. The live checks above are the deployment acceptance step.

## Limitations and roadmap

- Reviews diffs rather than full repository context; findings can be incorrect
- Publishes a conversation summary, not inline review comments
- Makes at most 30 model calls, with 20,000 characters per diff and 160,000
  characters total; skipped files remain visible in coverage
- Cannot review binary, empty, or otherwise unavailable patches as text
- Rejects PRs beyond GitHub's 3,000-file listing limit
- Does not automatically review fork, Dependabot, or draft PRs
- Certain specially quoted local Git filenames are not supported
- Local Git output is decoded as UTF-8 explicitly, including on Windows. Files
  in other text encodings must be converted to UTF-8 before local review; invalid
  bytes produce an input error rather than silently altering the reviewed code.

Roadmap:

- V1: Local Git diff review
- V2: GitHub PR input
- V3: Automatic PR summary comments with coverage and revision checks
- V4.1 (implemented): LangGraph state, conditional routing, bounded iteration,
  progress tracing, and a classic baseline
- V4.2 (planned): Controlled tool calling and revision-bound repository context
- V4.3 (planned): Retrieval experiments, review-quality evaluation, and execution records
- V4.4 (planned): Minimal API service, persisted task records, and Docker delivery
- V5 (planned): Durable queues, independent workers, recovery, and deployment scaling
