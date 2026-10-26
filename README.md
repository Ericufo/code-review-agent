# Code Review Agent

A Python tool that reviews local Git changes and GitHub pull requests with an
LLM. V4.2 adds opt-in, bounded repository tools to V4.1's LangGraph workflow.
Models can read source lines and search code at a pinned revision; findings cite
validated context evidence. The local CLI defaults to diff-only; the PR Review
workflow explicitly enables repository context.

## Features

- Reviews tracked and untracked local changes, or the changed files of a PR
- Validates structured findings with Pydantic
- Preserves successful results when other files fail
- Reports reviewed, failed, skipped, and scope-excluded file counts with reasons
- Paginates GitHub file/comment lists and rejects incomplete file retrieval
- Creates a marked PR summary comment and updates it on subsequent runs
- Records the reviewed head and base commits in the comment
- Cancels superseded Actions runs and checks revisions before publishing
- Includes mocked tests that need no API keys or network access
- Runs a bounded LangGraph workflow, with a classic V3 engine for comparison
- Shows node transitions and budget counters with `--trace`
- Enables read-only `read_file` / `search_code` with `--context repo`
- Pins PR tools to head/base Git objects and local tools to an in-memory snapshot
- Checks evidence against actual tool results and treats tool errors as incomplete

## V4.3 evaluation baseline

The evaluation package compares diff-only review with repository-context review
on 20 frozen synthetic cases. It includes offline execution checks, an opt-in
live runner with request/cost limits, and an adjudication importer. Install the
dependencies using [Setup and manual use](#setup-and-manual-use) first. Run the
commands below from the repository root with the virtual environment active.

### Evaluation data and labels

[cases/](evals/cases/) contains complete base/head source snapshots, changed
files, expected defects, triggers, and supporting evidence. The dataset has
10 defects and 10 clean changes: development 12 (6/6), holdout 8 (4/4), grouped
by scenario family. The model receives only diffs and permitted source context,
not case names, labels, or expected answers.

[dataset-lock.json](evals/dataset-lock.json) freezes version
`v4.3-synthetic-20-v1`, case hashes and splits.
[behavior.json](evals/behavior.json) supplies executable base/head probes used
by the tests. Changing source or labels requires versioning and refreezing the
dataset; do not replace the lock merely to silence a mismatch.

The supplied labels were AI-reviewed; the dataset maintainer accepted that review
and waived independent human sign-off. [annotation-review.json](evals/annotation-review.json)
records the decision against the frozen dataset hash and is read by the live
runner. This is label provenance, not automatic approval of model findings.
Re-running the experiment reproduces the procedure and generates new results;
model randomness, provider alias changes and adjudication can change scores.
Historical reports, run outputs and working notes are local artifacts, excluded
from version control and unnecessary for executing the evaluation.

### Offline checks

```bash
python -m evals.dataset
python -m evals.runner --split all
python -m unittest discover -s tests -v
```

These commands need no credentials, make no model requests, and publish no
comments. The dataset validator checks all 20 cases and their lock. The runner
uses the production graph with scripted responses: `--split all` produces 40
case-mode records plus five failure injections. Expected `INCOMPLETE` fault
records remain incomplete; success means their behavior matches expectations.
Without `--split`, the runner defaults to dev (29 records including faults).
`--suite cases` omits fault injections; `--suite reliability` runs only those.

The runner prints a new output directory under ignored `evals/runs/`, containing
`manifest.json`, `results.json`, `events.jsonl`, `report.md`, and pending
`adjudications.json`. Use `--output PATH` only with a new directory. Mock outputs
test the pipeline, not model precision or recall.

### Bounded live evaluation

The tracked [example plan](evals/live-plan.example.json) selects two dev cases
and both modes (four records), with at most 16 requests, six requests per
case-mode and a $1 cost upper-bound budget. Copy it to a local directory before
editing. The following commands use Bash, including Git Bash on Windows:

```bash
mkdir -p evals/runs/plans
cp -n evals/live-plan.example.json evals/runs/plans/pilot.json
```

Before executing, check the [provider pricing and limits](https://api-docs.deepseek.com/quick_start/pricing/).
Update `pricing_checked_on` to the current **UTC** date, and verify
`input_usd_per_million`, `output_usd_per_million` and `provider_context_tokens`.
Use prices that bound peak/cache-miss usage and the model's actual input limit.
The checked-in values are a dated template, not a promise of current pricing.
Set `max_run_calls` and `max_cost_usd` explicitly for each run.

```bash
# Offline plan validation only; --live is required to spend API calls.
python -m evals.live --plan evals/runs/plans/pilot.json
```

Validation should report `live: false` and four records. To execute, set
`DEEPSEEK_API_KEY` in the environment; unlike `main.py`, this entry point does
**not** automatically load `.env`. The following reads it without echoing it:

```bash
read -r -s -p "DEEPSEEK_API_KEY: " DEEPSEEK_API_KEY
printf '\n'
export DEEPSEEK_API_KEY
# Paid model requests; runs only after the plan and budgets are configured.
python -m evals.live --plan evals/runs/plans/pilot.json --live
unset DEEPSEEK_API_KEY
```

Each run creates a new directory with the same artifact names as the offline
runner. Exit 0 means all planned records completed, not that findings are correct.
Exit 1 means incomplete/stopped execution; inspect the saved reasons. Invalid
plans, missing credentials or an outdated pricing check are rejected before
dispatch. The evaluation entry points never publish GitHub comments.

For the full comparison, copy the example to two new local plans and change:

| Setting | Formal dev | Formal holdout |
| --- | --- | --- |
| `split` | `"dev"` | `"holdout"` |
| `case_ids` | `[]` (all in split) | `[]` (all in split) |
| `holdout_config_frozen` | `false` | `true` |
| `max_case_calls` | `6` | `6` |
| `max_run_calls` | `96` | `64` |
| `max_cost_usd` | `"1.00"` | `"1.00"` |
| Planned records | 24 | 16 |

Keep the other model, tool, character and output limits from the example.
`mode_order` defaults to `["diff-only", "context"]`; either ordering is allowed
but must contain both modes exactly once. Diff-only uses at most two requests
(including one format repair), while context may use six, explaining the total
request caps above. Costs can stop a run earlier even when calls remain.
Validate each plan without `--live`, then run dev first. Freeze the final model,
prompts and per-case budgets before holdout; the two manifests should have the
same `comparison_config_hash`. Keep pilot attempts separate and never tune on
holdout or combine only successful records from different attempts.

Both modes use the same Responses transport with SDK retries disabled.
Before dispatch it reserves the full configured context cost plus maximum
output cost. Complete usage settles that reservation at the configured rates;
missing usage or request errors retain it and stop further requests. Exceeded
provider bounds stop the run with `bound_valid: false`. `cost_upper_bound_usd`
is a conservative estimate dependent on correct prices and provider limits;
`cost` remains null because billing is not observed. Budget headroom must cover
one full reservation, so small runs can stop before spending their nominal cap.

All planned records are registered before execution; each completed record is
checkpointed. Failures, interruptions and unstarted records retain their place
in the denominator. There is no automatic resume or retry of a failed run.
For live request counts and usage, use transport events and resource totals;
do not add graph-level `model_request` events to transport counts. Missing token
usage stays null; characters are not tokens. Events store hashes, counts,
timings and status, not prompts, raw provider responses or internal reasoning.

### Adjudication and scoring

Copy a live run's `adjudications.json` to `adjudications.reviewed.json` inside
that run directory. Review findings against the **same frozen fixture** and
execution results before editing the copy:

1. Preserve run/record/case IDs, case hashes and `rubric_version`. Fill `reviewer`
   and ISO `date`; identify AI reviewers explicitly. Mark a record `completed`
   only after reviewing all its findings, including checking empty results.
2. Match by root cause, relevant location, actual trigger and impact, rather
   than title equality or severity. Use `TP` with the matching `defect_id`, or
   `FP` with a concrete counterexample and null `defect_id`. Supply a substantive
   `reason`. Unclear claims or genuine defects outside gold remain `pending`.
3. Point duplicates directly to one representative using `duplicate_of`; their
   verdict, defect ID and evidence decision must agree. Each gold defect can
   receive at most one TP per record. For multiple independent claims in one
   finding, replace it with distinct IDs such as `f1.c1`/`f1.c2`, each carrying
   `source_finding_id: "f1"`; do not also score the original as a whole.
4. For context findings, set `evidence_valid` true only when citations have the
   correct snapshot/path/range, came from actual tool results, and together
   support the claim. Legal citations alone are insufficient. Diff-only uses
   null because it does not provide structured citations. Never edit raw results.

```bash
# Replace with the directory printed by the live runner.
EVAL_RUN_DIR='evals/runs/your-run-id'
python -m evals.score --run "$EVAL_RUN_DIR" --adjudications "$EVAL_RUN_DIR/adjudications.reviewed.json" --output "$EVAL_RUN_DIR/scored"
```

The output directory must be new. The scorer writes `scores.json` and `report.md`:
exit 0 means adjudication is complete, 1 means pending judgments remain, and 2
means invalid inputs/output path. Mock scoring is rejected unless `--allow-mock`
is explicitly supplied for a pipeline exercise.

Scores are grouped by split and mode, with all-case and completed-subset
denominators. A missed gold defect is FN only if its file was reviewed; otherwise
it is unobserved. A clean case is TN only after a complete, nonempty-eligible
review with zero findings; incomplete clean cases are unknown. Precision is
TP/(TP+FP); all-case recall is TP/all selected gold defects. Clean-case false
positive rate uses completed clean cases as its denominator. Zero denominators
are N/A, and any pending judgment suppresses final rates. Report coverage,
failures, evidence validity, duration, calls and cost alongside quality scores.

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

The model configuration is in `reviewer.py` (diff-only) and `context_review.py`
(repository mode). Diff contents and requested source context are sent to the
external model API.

```bash
# Review local changes
python main.py

# Review an open, non-draft PR and publish its summary
python main.py --github-pr 7

# Show LangGraph progress on stderr (local review)
python main.py --trace

# V4.2: review using bounded repository tools
python main.py --context repo --trace

# Optional explicit budgets for a larger run (characters, not tokens)
python main.py --context repo --max-model-calls 96 --max-input-chars 3000000 --max-context-chars 384000 --max-tool-calls 192 --trace

# Same tool flow on a PR; this command publishes a summary comment
python main.py --github-pr 7 --context repo --trace

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

`--context repo` requires `--engine langgraph`. It sends selected repository
source to the external model as well as the diff. No extra dependency is needed
when upgrading V4.1. The PR Review workflow explicitly enables `--context repo`
with bounded budgets and `--trace`. The Chinese walkthrough
(`docs/v4-part2-walkthrough.md`) and closeout record (`docs/v4.2-closeout.md`)
are local learning notes: `docs/` is intentionally ignored and is not included
in a fresh clone. Development probes, synthetic cases and raw run records in
`experiments/` are also local-only; the agent and its CI tests do not depend on them.

Repository mode permits common source extensions (Python, JS/TS, Java, C#, Go,
Rust, C/C++, Ruby, PHP, Swift, Kotlin and SQL). Hidden paths, secret/credential
filenames, dependency directories and other extensions are excluded. This is a
path policy, not a scanner for secrets hardcoded inside otherwise allowed code.
Excluded changed files appear separately as `EXCLUDED`, before patch/size/budget
checks, and are removed from the eligible coverage denominator. They do not make
a batch incomplete. Eligible failures or budget skips still produce `INCOMPLETE`;
an empty eligible set produces `NO_ELIGIBLE_FILES`, not a claim that code was reviewed.
Search absence applies only to the
allowed source scope, not to the entire repository.

For explicit local batches, use `python main.py --context repo --paths main.py
review_graph.py`. Paths must match current Git changes exactly. The output labels
the selection; context still includes the allowed source snapshot. This is not a
persistent resume cache. `--paths` cannot be used with PRs. For PR verification
without writing a comment, use `python main.py --github-pr 7 --context repo
--no-publish`; revision checks still run before and after review.

Local snapshots capture tracked and unignored untracked source, use HEAD as
base, reject symlinks/reparse points, and check source fingerprints before and
after review. A final diff comparison rejects changed input. PR tools use GitHub
tree/blob APIs at exact head/base SHAs and validate blob hashes; the checkout's
merge commit is never used as context. Fork head objects unavailable through the
target repository API fail explicitly. Source snapshots support UTF-8 only and
have limits of 500 files, 128,000 bytes per file and 2,000,000 bytes per version.

Repository mode defaults to these independent batch budgets:

| Budget | Default | CLI override |
| --- | ---: | --- |
| Model requests, including final answers and format repair | 64 | `--max-model-calls` |
| Tool executions | 160 | `--max-tool-calls` |
| Returned tool context characters | 256,000 | `--max-context-chars` |
| Cumulative serialized input characters, including instructions, tools and replayed history | 2,000,000 | `--max-input-chars` |

The file limit remains 30. Each file permits 6 normal model turns, at most one
additional tools-disabled format repair, 24 tool executions and 96,000 context
characters. The CLI explains when the configured call budget cannot cover seven
requests for every candidate (other limits may bind earlier).
Reads return at most 120 lines per page, even for larger
requested ranges. Results page at 12,000
characters, shrinking further to fit the remaining file/batch context budget;
searches also page at 20 matches and accept an optional exact `path`
filter and `offset`. Optional `context_lines` (a nonnegative integer, default 0)
includes nearby lines so a definition and its signature can be inspected in one
call. Requests above 20 are clamped to 20 lines on each side. Search
entries identify `match_line`; `context_truncated` means optional neighbors were
reduced to fit the budget. Evidence covers only the lines actually returned.
Offsets count matches, even when context ranges overlap.
Check `has_more` and use `next_start_line` / `next_offset` to
continue. Pages contain only complete lines and evidence describes only returned
ranges. Partial search results are not exhaustive. A single oversized line/match
or an exhausted cumulative context budget still fails explicitly. Reads clamp at
EOF and report the actual line count.

The last available model turn is reserved for a final answer. Tools are removed
from that request, also when no tool calls remain; `--trace` shows
`finalize_request`. Each request explains its remaining budgets and recommends
short, targeted reads. Final responses must explicitly report `context_complete`;
missing essential context stays incomplete, even with an empty issue list.
Earlier tool failures also remain failures. Raising the model request budget does
not raise the other limits or guarantee that every file will finish.

Model requests use JSON mode, low reasoning effort and a 16,384 output-token cap
(reasoning and visible output share that cap). Final-message text is parsed
separately from commentary; a whole JSON code fence is tolerated. One invalid or
output-truncated answer may receive one tools-disabled repair request, including
after the last normal round. This still charges the batch call/input budgets and
must fit the file deadline; it does not reopen tools. Repeated invalid answers, filtered responses and
fabricated evidence remain failures. SDK network retries are disabled.

Requests have a 45-second SDK timeout. Each file also has a 180-second elapsed-time
budget checked between operations; in-flight
network calls may finish after that deadline. Any tool error makes that file
incomplete even if the model later returns no issues. Other files can still
succeed. Evidence verification proves the cited range was supplied, not that the
model's reasoning is correct. Trace `attempted` counts files; `model_calls` and
`tool_calls` count the V4.2 subgraph requests separately. Trace shows input/context
character counters and their limits, and emits `repair_request` for format repair.
A budget rejection before the first model request is skipped; rejection after a
file has started is failed. Input-budget errors report used, next-request and
limit values, separately from model-call exhaustion.

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
| `PR Review` (`review.yml`) | PR opened, synchronized, reopened, or marked ready for review | Uses repository context and creates or updates the summary |

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
- An eligible failed or skipped file makes coverage **INCOMPLETE** and exits with code `1`.
  Scope exclusions are separate; no eligible files returns **NO_ELIGIBLE_FILES** with code `0`.
  Successful findings and failure/skip reasons are still published when possible.
- GitHub retrieval or publication failures also exit with code `1`.
- Closed/draft PRs and superseded runs are explicitly skipped with code `0` and
  do not publish. A successful check alone does not prove a review ran; check the
  log and the comment's commit SHA.
- Each review job has a 30-minute timeout. A cancelled or timed-out job may leave
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
python main.py --github-pr "$PR_NUMBER" --expected-head-sha "$EXPECTED_HEAD_SHA" \
  --context repo --trace \
  --max-model-calls 384 --max-tool-calls 1024 \
  --max-context-chars 4000000 --max-input-chars 24000000
```

Actions uses these explicit larger limits; the CLI defaults above remain unchanged.
On 2026-10-21, these batch limits were doubled again to leave extra headroom for
the V4.2 closeout. Per-file limits remain 24 tools and 96,000 context characters;
the expanded-budget live run did not exhaust those limits. Higher limits allow
more spending if needed; they do not guarantee completion or resolve connection,
output-format, or unavailable-source failures.
These are batch limits, not a promise to finish every PR size. On 2026-10-20,
[PR #4's first hosted run](https://github.com/Ericufo/code-review-agent/actions/runs/39398900386)
updated its summary comment but exited with `INCOMPLETE`: 6/21 eligible files
reviewed, 4 failed, 11 skipped, and 4 excluded. Two failures reported missing
context; two failures and all skips hit the old 2,000,000 input-character limit.
The run consumed 47/64 model requests, so model-call exhaustion was not the
immediate cause. The larger Actions limits address that observed batch size;
the model can now use its last remaining tool call before the reserved final
answer turn. A subsequent full-PR, no-publish run with the first repair completed
15/21 files with no skips (93 requests, 3,674,723 input characters); its six
failures all concerned the former eight-tool per-file limit or missing context.
The second repair adds bounded search context and raises that per-file limit to
12. A new hosted run is still needed to validate the updated configuration.

[The next hosted run](https://github.com/Ericufo/code-review-agent/actions/runs/39433088720)
completed 13/21 files with eight failures and no skips. Its batch budgets were
not exhausted: three files failed because requested search context exceeded 20,
three exhausted the per-file 48,000-character context limit, and two exhausted
12 tool calls. The latest adjustment clamps oversized search-context requests
instead of rejecting them, doubles per-file context/tool limits, and increases
the explicit Actions budgets shown above. Failure reporting remains unchanged.

Manual reviews also check revisions, starting from the current head rather than
an event's expected head. Base-only updates and retargeting are not automatic
triggers in this version; re-run the current job when needed.

These checks reduce stale writes but are not atomic: the PR can change between
the final check and the comment request. The comment's head/base SHA identifies
the reviewed revision. Independent manual invocations are not serialized by
Actions concurrency.

## Shared application service (V4.4.1)

`review_service.py` provides `ReviewConfig`, `PreparedReview`,
`prepare_local_review`, `prepare_github_review`, and `execute_review` for CLI and
future API callers. The CLI uses this service; printing, exit codes, and comment
publication stay in `main.py`. Importing the service does not load `.env`, start
threads, create clients, or perform network/model requests. The CLI loads its
environment when `main()` runs. This core service does not import the optional
API/database packages. PostgreSQL storage, the background executor, and HTTP
routes are described below.

An offline call with an injected model, requiring no credentials:

```python
from diff_parser import FileDiff
from review_models import ReviewResult
from review_service import PreparedReview, ReviewConfig, execute_review

work = PreparedReview(
    files=(FileDiff("example.py", "@@ -1 +1 @@\n-old()\n+new()"),),
    config=ReviewConfig(),
)
batch = execute_review(
    work, review_fn=lambda diff: ReviewResult(summary="", issues=[]),
)
print(batch.status)  # COMPLETE: deterministic mock, not a real review
```

Repository-mode callers supply `ReviewConfig(context_mode="repo")`, a
`RepositoryContext`, and `context_request_fn` for offline execution. Without
injected model functions, execution uses the existing configured model paths.
Preparation accepts injected file loaders and context factories; tests must
replace GitHub access as well as the model to be fully offline. PR preparation
captures head/base and rejects a changed revision after listing files. That
mutable PR-list endpoint is not an immutable diff provider for future queued
tasks; queued execution must use its saved revision rather than prepare a new PR.

Optional `sink` implementations follow `ReviewSink` in `review_checkpoint.py`:

- `record_event(event)` receives non-completion events.
- `file_completed(checkpoint)` receives the authoritative path, reviewed/failed/
  skipped/excluded status, validated findings and evidence, safe reason code,
  and the corresponding completion event. Persist these together; the completion
  event is not also sent through `record_event`.
- `finish(batch)` is called after final input validation and must commit the
  final summary and task status together. A batch can be `INCOMPLETE` or
  `NO_ELIGIBLE_FILES`; successful execution does not mean defect-free code.

A sink error latches `ReviewPersistenceError` (`code="persistence_failed"`),
aborts further work, and prevents a successful return. Previously saved file
results remain the sink's responsibility. Ordinary `on_record` observers retain
their warning-and-continue behavior. Event counters can be cumulative; missing
usage stays unknown. These contracts do not provide database transactions or
restart/resume by themselves. The CLI uses the no-persistence path and still
needs only the core dependencies.

## PostgreSQL storage (V4.4.2)

Install the optional service dependencies in the existing virtual environment:

```bash
python -m pip install -r requirements-api.txt
```

This includes pinned SQLAlchemy 2.x, psycopg 3, Alembic, FastAPI/Uvicorn, and
Starlette's actual `httpx` TestClient dependency (`httpx2` is a different package).
The tested combination requires Python 3.11 or newer; local verification used
Python 3.13.5. `pip check` and a real in-process FastAPI TestClient smoke test pass.
Installing these dependencies does not start an HTTP service.

`review_api/models.py` defines tasks, files, findings, and events.
`ReviewRepository` provides submission/idempotent replay, a single active task
claim, atomic file/finding/completion-event writes, final summaries, terminal
failure/interruption, and task/result/event queries. `find_replay` must be called
before fetching new PR metadata; its fingerprint depends only on the normalized
client request. Replays return the original task even after the PR or server
configuration changes. Admission and claim use short PostgreSQL transactions;
no GitHub or model calls run inside them.

Task transitions and immutable revision/config snapshots have database guards.
A partial unique index permits at most one running task. A file completion event
is stored in the same transaction as its file and findings. Finalization checks
that the supplied batch matches saved checkpoints, then commits the final
summary and `finished` together. Raw exception text is not stored: file failures
use safe reason codes. Task results distinguish execution completion from review
coverage. Missing usage remains null, and cumulative counters are not summed.

Each repository operation owns its Session. Task listing uses a `(created_at,id)`
cursor; events use per-task increasing sequence numbers. Task pages allow 1–100
items and event pages 1–500. Storage accepts normalized relative paths up to
512 characters, rejecting longer paths explicitly rather than truncating them.
`schema_ready(engine)` checks the applied Alembic revision without modifying it.

For a separately provisioned application database, set `DATABASE_URL` to a
`postgresql+psycopg://USER:PASSWORD@HOST:PORT/DATABASE` URL with a nonempty password,
then explicitly apply migrations:

```bash
python -m alembic upgrade head
```

The migration command reads the process environment, not `.env` automatically.
It does not run inside API requests. Repeating `upgrade head` is harmless.
No application database or production Compose setup is created by this step.

### Dedicated PostgreSQL tests

With Docker Desktop running in Linux-container mode, a native PostgreSQL install
is unnecessary. From the repository root, the following helper creates a local
test container using the verified PostgreSQL 18.6 image digest:

```bash
python scripts/start_test_postgres.py
python scripts/run_postgres_tests.py --env-file .env.pgtest
```

The startup helper binds a random port on `127.0.0.1`, generates a password, and
stores test credentials in ignored `.env.pgtest`. It refuses to overwrite an
existing configuration or container; an already configured environment can run
the second command directly. The container uses a temporary memory filesystem:
stopping/restarting it loses its database contents. This is intentional for
tests and is not the persistent application deployment planned for V4.4.5.
The [official PostgreSQL image](https://hub.docker.com/_/postgres) uses
`/var/lib/postgresql` as its volume target for PostgreSQL 18 and newer.

The test runner accepts only a local `review_test_admin` database URL, creates a
new `review_test_<uuid>` database, applies migrations there, and drops only that
new database on exit. It never migrates or clears the supplied admin database.
It cannot use a development database URL to clear application records. You may
instead set `REVIEW_TEST_DATABASE_URL` in the process environment and omit
`--env-file`. Database failures fail this command; they are not silently skipped.

The PostgreSQL checks cover empty/repeated migrations and schema drift,
concurrent idempotency/admission/claim/event writes, transaction rollback,
invalid transitions, immutable snapshots, duplicate keys, partial results,
pagination, mock-service integration, and dependency/test-environment safety.
Run core regressions separately with `python -m unittest discover -s tests -v`;
they remain usable without the optional storage installation. These database
tests live in `tests/postgres/` and are deliberately outside core discovery.
They also exercise executor ownership, startup interruption, lost notifications,
process termination, persistence failures, and bounded shutdown with mock inputs.
HTTP integration tests use that same real database with fixture upstreams.
Container acceptance is described below. The live model demonstration remains a later step.

## Background executor (V4.4.3)

`review_api.executor.ReviewExecutor` runs one task at a time in a single daemon
thread. Construction and imports do not start it. Application lifecycle code
must call `start()` and `stop()` outside the ASGI event loop. The V4.4.4 application
factory below provides this lifespan wiring.

Startup acquires a dedicated PostgreSQL session advisory lock and checks the
migration revision before marking old `queued`/`running` tasks `interrupted`.
Only then does `start()` return true. A second executor returns false with
`failure_code="executor_busy"` and does not alter existing tasks. The lock
connection is never pooled or reconnected. Committed database rows drive
scheduling; `notify()` only wakes the worker sooner, and polling handles lost
notifications. Submit through `executor.submit(...)` to enforce the readiness
gate inside the admission transaction as well as before it.

The required server-owned `prepare(task, before_work)` dependency must produce
a `PreparedReview` using the task's saved head/base and configuration, and call
`before_work()` before each external request during preparation. The executor
rejects mismatching revisions/configuration, local repository contexts, and
CLI validation callbacks. It also guards context reads and model/tool dispatch.
Tests inject repository fixtures; V4.4.4 uses a read-only GitHub adapter for the
normal server. Do not use `prepare_github_review` to silently refresh the revision
of an already queued task.

Model mode comes from the saved configuration. Mock mode uses a deterministic
empty result, labelled `mock` in storage, without falling back to environment
credentials. Live mode is rejected unless both `allow_live=True` and an explicit
model transport are supplied. That transport must enforce timeouts and visible
retry accounting; the executor does not make an arbitrary callable interruptible.
The mock result validates plumbing, not the quality of a PR review.

`DatabaseReviewSink` commits events, file checkpoints, and the final summary
through the repository. A persistence or ownership failure clears readiness and
blocks subsequent dispatch at the next guarded boundary. An in-flight request
cannot be recalled; missing resource records remain explicitly incomplete.
Already committed files survive. A normal per-file model error can still produce
`finished` with `INCOMPLETE` coverage; a persistence failure cannot produce a
successful final summary. Old work is never automatically retried on restart.

`request_stop()` stops admission and new work while allowing an in-flight response
to be saved. `stop(grace=10)` waits at most that many seconds (0–60 permitted).
It returns true when the worker has exited; false means the grace expired.
After expiry, late checkpoints are rejected, but the worker retains its session
lock until it actually exits or its process ends. Python cannot forcibly stop
an arbitrary blocked request. The process supervisor must finish shutdown when
`stop()` returns false; the next lock-owning startup interrupts leftover tasks.
Do not start a replacement worker in the same process after a false result.

This is a single-instance guard, not a distributed lease or exactly-once protocol.
The tests use an isolated database and include killing a separate worker process
after its first file is committed; this does not establish container-volume
durability; the separate Compose acceptance below covers that boundary.

## HTTP task API (V4.4.4)

`review_api.app.create_app()` constructs the FastAPI app; lifespan creates its
database repository and executor. Imports do not load environment files, connect,
or start threads. Startup does not apply migrations. Synchronous routes and
executor start/stop waits run outside the ASGI event loop. The API only analyzes:
it has no comment publication, cancel, retry, or arbitrary local-path endpoint.

Configure a separately provisioned application database and server environment
using `.env.example`. The existing `.env.pgtest` is only for isolated tests.

| Setting | Meaning |
| --- | --- |
| `DATABASE_URL` | Required PostgreSQL URL with password; see storage setup above |
| `REVIEW_API_TOKEN` | Required nonempty ASCII bearer token, without whitespace |
| `REVIEW_ALLOWED_REPOS` | Required comma-separated `owner/repo` allowlist; case normalized |
| `GITHUB_TOKEN` | Server credential for GitHub reads; optional for public repositories |
| `REVIEW_MODEL_MODE` | `mock` by default; `live` explicitly enables the configured model |
| `DEEPSEEK_API_KEY` | Required only for `live`; never accepted in a request body |
| `REVIEW_PROFILES_JSON` | Named budget overrides, default `{}` creates profile `default` |
| `REVIEW_WAITING_LIMIT` | 1–8 waiting tasks, default 8; one active task |
| `REVIEW_GITHUB_TIMEOUT` | Request timeout in seconds, default 10, maximum 30 |
| `REVIEW_MODEL_TIMEOUT` | Model request timeout in seconds, default 45, maximum 60 |
| `REVIEW_MAX_OUTPUT_TOKENS` | Output cap, default/maximum 16384 |
| `REVIEW_SHUTDOWN_GRACE` | Shutdown wait in seconds, default 10, range 0–60 |

Profiles can lower the core ceilings: 30 files, 20,000 diff characters per file,
160,000 total diff characters, 64 model calls, 2,000,000 input characters,
256,000 context characters, and 160 tool calls. For example:
`{"default":{"max_files":10,"max_model_calls":20},"small":{"max_files":3,"max_model_calls":6}}`.
Unspecified values retain defaults. Model mode is server-wide; clients choose a
profile name and cannot override budgets, providers, keys, model URLs, or publish.
These are call/character/token caps, not a dollar spending ledger.

If the variables are already exported to your shell, migrate and start with:

```bash
python -m alembic upgrade head
python -m uvicorn review_api.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

For an explicitly chosen local `.env` file, load it for each entry point:

```bash
python -c "from dotenv import load_dotenv; from alembic.config import Config; from alembic import command; load_dotenv('.env'); command.upgrade(Config('alembic.ini'), 'head')"
python -m uvicorn review_api.app:create_app --factory --env-file .env --host 127.0.0.1 --port 8000 --workers 1
```

Use one worker and no reload. Failed database/schema/ownership startup leaves
`/health/live` available, but `/health/ready` returns 503 and new submissions are
rejected. Shutdown grace expiry raises a safe lifespan error so the server process
can exit; the old worker retains its lock until it exits. Restart never resumes
an unfinished task. Compose startup and volume handling are described below.

| Route | Result |
| --- | --- |
| `POST /api/v1/reviews` | 202 new task; 200 idempotent replay |
| `GET /api/v1/reviews?limit=20&cursor=...` | Task page and `next_cursor`; maximum 100 |
| `GET /api/v1/reviews/{id}` | Execution/review state, versions, config, counts and timestamps |
| `GET /api/v1/reviews/{id}/result` | Saved files/findings/evidence, `partial`, model mode and resource completeness |
| `GET /api/v1/reviews/{id}/events?after_seq=0&limit=100` | Ordered events, `has_more`, `next_after_seq`; maximum 500 |
| `GET /health/live` | Unauthenticated liveness, no configuration details |
| `GET /health/ready` | Unauthenticated database/schema/executor readiness |

All `/api/` requests require `Authorization: Bearer <REVIEW_API_TOKEN>`.
This is one trusted caller group: anyone with that token can read all stored
tasks. It is not per-user authentication or tenant isolation. Swagger/OpenAPI
HTTP endpoints are disabled; DTOs and route definitions live in `review_api/`.

Example request (replace the token and allowlisted repository):

```http
POST /api/v1/reviews HTTP/1.1
Host: 127.0.0.1:8000
Authorization: Bearer <REVIEW_API_TOKEN>
Idempotency-Key: 51f9d728-f943-48e9-9368-f9cc9aba514d
Content-Type: application/json

{"owner":"allowed-owner","repo":"allowed-repo","pr_number":5,"profile":"default"}
```

The response contains `task_id`, `execution_status`, `model_mode`, head/base SHAs,
and relative `status_url`, `result_url`, `events_url`. Poll the status URL using
the same bearer token until `finished`, `failed`, or `interrupted`, then fetch the
result URL. Partial files may be queried sooner. `finished` with `INCOMPLETE`
does not mean review passed, and `NO_ELIGIBLE_FILES` is not a defect-free verdict.

The UUID idempotency key is mandatory. The same key and normalized request return
the original task before reading current PR metadata or resolving changed budget
profiles, even if the queue is full. Different input with the same key returns
409. Supply a new key for a new review. Optional `expected_head_sha` must be a
40-character lowercase SHA; a mismatch, draft, or closed PR returns 409 without
creating a task. Unknown body/query fields are rejected. POST bodies are capped
at 16 KiB, including streamed requests. No result truncation is hidden.

Errors contain only `code`, `message`, and `request_id`, with the same generated
ID in `X-Request-ID`: 401 authentication, 403 repository denied, 404 unknown task,
409 conflict/unreviewable PR, 413 oversized body, 422 invalid input/profile/cursor,
429 queue full, 502 upstream metadata failure, 504 upstream timeout, 503 unavailable
service/storage, or 500 unexpected internal failure. Responses do not echo input,
tokens, database errors, source text, or upstream exception bodies.

`GitHubProvider` reads PR metadata once at submission, then uses an immutable
`compare/{base_sha}...{head_sha}` request and exact Git trees/blobs. It does not
follow redirects, has no publication methods, caps each response at 8 MB, and
uses no automatic retries. A comparison containing 300 or more files fails the
task: the [GitHub comparison endpoint](https://docs.github.com/en/rest/commits/commits#compare-two-commits)
only supplies up to 300 files, so this boundary cannot establish a complete list.
Oversized/truncated source trees are rejected by the existing context policy.
Unavailable patches remain explicit skipped files rather than clean reviews.

`mock` disables model requests but the normal server still reads GitHub. Fully
offline HTTP tests inject a fixture provider as well as the mock model. Live
transport uses the server key, fixed DeepSeek endpoint/model, configured timeout
and output cap, and `max_retries=0`; the API does not import the evaluation ledger.
No live GitHub or paid model calls were used for V4.4.4 verification. Its checks
cover the real database, real executor and ASGI app, plus offline provider tests;
they do not establish real credential validity or hosted deployment readiness.

## Docker Compose deployment (V4.4.5)

Docker Desktop must be running Linux containers, or use Docker Engine with
Compose v2 on Linux. Native PostgreSQL is not required. The application image
uses the verified Python 3.13 slim Bookworm digest; PostgreSQL 18.6 is also pinned
by digest. The image runs as UID/GID 10001 with a read-only application filesystem,
and its build context uses an allowlist that excludes credentials, Git history,
virtual environments, personal documents, evaluation outputs, and test fixtures.

Copy `.env.compose.example` to an untracked `.env.compose`, generate separate
random database/API secrets, and set `REVIEW_ALLOWED_REPOS`. Keep model mode
`mock` until a live run is explicitly intended. Generate each secret with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

The Compose launcher constructs `DATABASE_URL` from the database fields with
proper password quoting; do not copy a host `DATABASE_URL` into the container.
No credentials are passed as build arguments or embedded in the image. A normal
mock deployment still reads GitHub on a new submission; fully offline fixtures
are exclusive to the acceptance runner described below.

```bash
docker compose --env-file .env.compose up --build -d --wait
docker compose --env-file .env.compose ps -a
```

Startup follows **healthy database → successful one-time migration → API**.
No manual schema creation is needed. The API health check probes `/health/ready`;
failed migrations prevent API startup. The API is published only on
`127.0.0.1:8000` (override `REVIEW_API_PORT` if occupied); PostgreSQL has no host
port. Use the HTTP submission and polling instructions above. The API container
has one Uvicorn worker, no reload, and a 90-second Docker stop grace period to
allow the bounded application shutdown to finish.

PostgreSQL data is stored in the named `review-data` volume at
`/var/lib/postgresql` inside the PostgreSQL 18 container. Its actual volume name
is prefixed by the Compose project name, normally `code-review-agent_review-data`.
Keep the same project name and credentials to reuse it. These routine commands
preserve the data:

```bash
docker compose --env-file .env.compose restart api
docker compose --env-file .env.compose down
docker compose --env-file .env.compose up -d --wait
```

**`down -v` deletes the database volume and its saved results.** It is not a
routine restart or troubleshooting command. A volume preserves data across
container recreation; it is not a backup. The isolated acceptance runner deletes
only its own randomly named test volume after checking its ownership.

For a database restart, stop API first, restart DB, then run `up -d --wait` to
restore dependency ordering. Restarting DB alone can invalidate the executor's
ownership connection; an unhealthy API then needs a fresh process. For an image
upgrade, stop API, rebuild, and use `up -d --wait` so the migration gate is applied.
Do not scale API replicas or run a second server against the same task database.

Troubleshooting:

- Inspect `docker compose --env-file .env.compose ps -a` and
  `docker compose --env-file .env.compose logs --tail=100 db migrate api`.
  Avoid sharing `docker inspect` or resolved Compose configuration: environment
  variables there contain runtime secrets.
- An exited migration service with code 0 is normal. A nonzero exit blocks API;
  correct credentials/schema issues before starting it again.
- Changing `POSTGRES_PASSWORD` in the env file does not rotate the existing
  database role's password. Keep the original value until explicitly rotating
  credentials in PostgreSQL; do not delete the volume to fix a login error.
- Live health with failing readiness indicates database/schema/executor trouble.
  A second API instance intentionally stays unready. Stop the unintended instance
  and restart the intended process after correcting the cause.
- Compose does not restart a container merely because its health check fails.
  The `unless-stopped` policy handles process exits; diagnose unhealthy running
  services rather than waiting for an automatic recovery.

### Isolated, fully offline container acceptance

With Docker running, this helper builds the same production image and validates
it with a separate Compose project, random credentials, an unpublished database,
and an internal network that blocks upstream egress during fixture execution:

```bash
python scripts/verify_compose.py
```

Use `--skip-build` only if `code-review-agent:v4.4` already matches the current
source. The helper never reads your `.env`, `.env.compose`, or `.env.pgtest`. It
mounts the fixed `tests/compose/compose_fixture.py` read-only for HTTP mock work;
the production image contains no fixture/provider switch or fixture-upload API.
On internal networks, requests use real HTTP on container loopback; no host port
is needed for this phase. It checks fresh migration/health, saved results across API and DB restarts,
forced termination after a file checkpoint, data retention through `down/up`,
and a real migration failure preventing API startup. It finally uses a normal
bridge network and random host loopback port to verify the production entrypoint
without the fixture, only querying/replaying existing tasks. That phase performs
no new review and requires no GitHub call. The report and redacted Docker log stay under ignored
`reports/review-v445-test-<id>/`; generated test credentials are removed at exit.

### Continuous integration

`tests.yml` retains the core-only Python 3.11 job and adds two independent jobs:

- Python 3.13 + a real PostgreSQL 18.6 service runs the isolated database/API
  suite, frozen dataset validation, and 45 offline mock evaluations.
- A Docker job builds the image and runs the same offline Compose acceptance.

These jobs use read-only repository permissions, test-only database credentials,
blank upstream keys, and no model calls or image publishing. Workflow execution
on GitHub requires a later user-controlled push; local verification does not
claim a hosted CI pass. The existing separate PR Review workflow is unchanged:
it can call the paid model and publish a comment on eligible non-draft PRs when
its secret is configured. Keep the delivery PR as a draft while only offline CI
is authorized; marking it ready for review can trigger that workflow.

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
                 review_service.py
               prepare / configure / execute
                        |
                review_pipeline.py
              shared budgets / validation
                        |
         review_graph.py (default CLI) or classic loop
          check -> review / skip / exclude -> check -> summarize
                        |
           optional context_review.py subgraph
              model -> review_tools.py -> model
                        |
             repository_context.py snapshots
                        |
                    reviewer.py
                   model request
                        |
                review_models.py
                Pydantic validation
                        |
                    ReviewBatch
          findings + coverage + failures/skips/exclusions
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

Validation on 2026-10-20: the initial Python Tests workflow passed in Ubuntu Actions.
After the Actions failure investigation, 179 tests ran locally: 178 passed and
one Windows symlink-permission test was skipped. New regressions cover the last
available tool call, search context and evidence bounds, budget shrinking and
pagination with overlapping context, oversized search requests, and final-round
format repair without bypassing batch limits. The final normal model turn remains reserved.
Both workflows passed actionlint, and the updated review command was parsed
with the actual CLI. Two tests of local
experimental scripts remain outside the CI suite. Hosted comment publication
worked for the initial run, but full-PR completion and hosted validation of the
repair remain separate acceptance checks.

The expanded-budget full-PR run completed 17/21 files with no skips, using 113
model requests, 196 tool calls and 5,491,910 input characters. Four files failed:
two invalid final JSON answers, one output-token truncation and one disallowed
source request. The output cap was then doubled and a bounded format repair was
enabled after the last normal turn. A targeted three-file verification of those
output failures was blocked by two model connection errors and one GitHub API
error. It completed 0/3 and does not establish the final repair's live coverage.
These separate runs cannot be combined into a full-PR pass. Original logs and
diagnostics remain local in `experiments/`; hosted acceptance is still pending.

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

- Local CLI is diff-only by default; Actions enables bounded repository context.
  Findings in either mode can be incorrect.
- Publishes a conversation summary, not inline review comments
- Diff-only permits 30 model calls; repository mode defaults to 64 requests across
  at most 30 files. Both keep 20,000 characters per diff and 160,000 diff characters total
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
- V4.2 (implemented): Controlled read/search tools, revision-bound source snapshots,
  evidence validation and bounded model/tool subgraph; development cases kept locally
- V4.3 (implemented): Twenty frozen evaluation fixtures, structured events, offline
  and budgeted live runners, and adjudication-based scoring. The live baseline was
  completed with AI-reviewed labels accepted by the user; no independent human review.
- V4.4.1 (implemented): Shared application service, injected dependencies, and
  typed file-completion checkpoints with fatal persistence failure handling
- V4.4.2 (implemented): PostgreSQL schema/migrations, transactional repository,
  and isolated real-database tests
- V4.4.3 (implemented): Single-thread executor, session ownership lock,
  reliable persistence sink, startup interruption, and bounded shutdown
- V4.4.4 (implemented): HTTP task submission/query API, bearer authentication,
  repository restrictions, immutable GitHub reads, and lifecycle integration
- V4.4.5 (implemented): Docker/Compose, migration and health gates, persistent
  database volume, isolated container acceptance, and offline CI jobs
- V4.4.6 (planned): Stage acceptance and separately authorized live demonstration
- V5 (planned): Durable queues, independent workers, recovery, and deployment scaling

## Files to commit

Keep application code, agent tests, workflows, public `evals/` fixtures and rules,
`requirements.txt`, `.env.example` and this README. Agent tests verify behavior and failure handling without
depending on local experiments.

The entire `experiments/` directory, virtual environments, Python/test caches,
coverage output, real `.env` files and local `docs/` notes are ignored. Experiments
record development progress and diagnostics; they are not a formal benchmark.
V4.3 includes a frozen development/holdout evaluation set; see the evaluation guide
above. Historical runs and reports remain local. Existing tracked files are not
untracked by adding an ignore rule.
