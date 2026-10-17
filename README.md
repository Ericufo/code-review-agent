# Code Review Agent

A Python tool that reviews local Git changes and GitHub pull requests with an
LLM. V3 adds automatic PR reviews through GitHub Actions, reusable summary
comments, explicit review coverage, and checks for obsolete results.

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

## Setup and manual use

Python 3.10+ and Git are required. Actions uses Python 3.11.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` and copy the
configuration with `Copy-Item .env.example .env`.

Fill in `.env` for manual use; keep it out of Git:

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
```

Comments contain the hidden marker `<!-- code-review-agent -->`. Subsequent
runs update the marked comment; unmarked comments are preserved. If the PR has
no changed files, the comment is updated to say so instead of retaining old findings.

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
                  per-file budgets
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
- Makes at most 30 model calls, with 20,000 characters per diff and 80,000
  characters total; skipped files remain visible in coverage
- Cannot review binary, empty, or otherwise unavailable patches as text
- Rejects PRs beyond GitHub's 3,000-file listing limit
- Does not automatically review fork, Dependabot, or draft PRs
- Certain specially quoted local Git filenames are not supported

Roadmap:

- V1: Local Git diff review
- V2: GitHub PR input
- V3: Automatic PR summary comments with coverage and revision checks
- V4: Repository context, controlled agent tools, and review-quality evaluation
- V5: API service, persisted jobs, workers, and deployment
