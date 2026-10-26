import sys
import argparse
import json

from dotenv import load_dotenv

from review_pipeline import ReviewBatch, ReviewStep
from github_client import GitHubClient, GitHubAPIError
from github_comment import GitHubCommentManager
from github_review import (
    PullRequestRevision,
    ReviewSupersededError,
    ensure_current_revision,
)
from review_formatter import format_review_batch
from repository_context import allowed_path
from review_tools import ContextBudget, MAX_FILE_ROUNDS, MAX_FORMAT_REPAIRS
from review_service import (
    CONTEXT_BUDGET_OPTIONS, ReviewConfig, prepare_local_review,
    prepare_github_review, execute_review,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Code Review Agent"
    )

    parser.add_argument(
        "--github-pr",
        type=int,
        help="Review a GitHub pull request"
    )

    parser.add_argument(
        "--expected-head-sha",
        help="Skip an outdated Actions run if the PR head has changed",
    )

    parser.add_argument(
        "--engine", choices=("langgraph", "classic"), default="langgraph",
        help="Review orchestration (default: langgraph; classic keeps the V3 loop)",
    )
    parser.add_argument(
        "--trace", action="store_true",
        help="Print LangGraph node progress and budget counters to stderr",
    )
    parser.add_argument(
        "--context", choices=("diff", "repo"), default="diff",
        help="V4.2: enable bounded repository tools with repo (default: diff-only)",
    )
    defaults = ContextBudget()
    parser.add_argument('--paths', nargs='+', help='Local only: review these exact Git paths; context may include other source files')
    parser.add_argument('--no-publish', action='store_true', help='PR only: review without creating or updating a comment')
    for option in CONTEXT_BUDGET_OPTIONS:
        parser.add_argument('--' + option.replace('_', '-'), type=int, default=None,
            help=f"Batch {option[4:].replace('_', ' ')} budget for --context repo (default: {getattr(defaults, option)})")

    args = parser.parse_args()
    if args.paths is not None and args.github_pr is not None:
        parser.error('--paths is local-only; a partial selection must not appear as a full PR review')
    if args.no_publish and args.github_pr is None:
        parser.error('--no-publish requires --github-pr')
    if args.expected_head_sha is not None and args.github_pr is None:
        parser.error("--expected-head-sha requires --github-pr")
    if args.trace and args.engine != "langgraph":
        parser.error("--trace requires --engine langgraph")
    if args.context == "repo" and args.engine != "langgraph":
        parser.error("--context repo requires --engine langgraph")
    for option in CONTEXT_BUDGET_OPTIONS:
        value = getattr(args, option)
        if value is not None and (args.context != 'repo' or value < 1):
            parser.error('--' + option.replace('_', '-') + ' requires --context repo and a positive integer')
    return args


def print_review_step(step: ReviewStep) -> None:
    # JSON quoting prevents unusual filenames from adding terminal control lines.
    path = json.dumps(step.path, ensure_ascii=True)
    print(
        f"[GRAPH] {step.node} path={path} attempted={step.attempted} "
        f"reviewed={step.reviewed} failed={step.failed} skipped={step.skipped} excluded={step.excluded} "
        f"diff_chars={step.used_chars} model_calls={step.model_calls}/{step.model_limit} "
        f"tool_calls={step.tool_calls}/{step.tool_limit} "
        f"context_chars={step.context_chars}/{step.context_limit} "
        f"input_chars={step.input_chars}/{step.input_limit}",
        file=sys.stderr,
    )

def post_github_comment(
    client: GitHubClient,
    pr_number: int,
    batch: ReviewBatch,
    revision: PullRequestRevision,
) -> str:
    body = format_review_batch(batch)
    body += (
        f"\n\nReviewed commit: `{revision.head_sha}`\n\n"
        f"Base commit: `{revision.base_sha}`"
    )

    return GitHubCommentManager(client).publish(
        pr_number=pr_number,
        body=body,
        revision=revision,
    )

def print_review(batch: ReviewBatch) -> None:
    result = batch.result
    print()
    print("=== Review Result ===")
    status = batch.status
    print(f"Status: {status}")
    print(batch.coverage_summary)
    print(result.summary)

    if not result.issues:
        return

    for index, issue in enumerate(result.issues, start=1):
        print()
        print(
            f"{index}. [{issue.severity.upper()}] "
            f"{issue.title}"
        )

        location = issue.file

        if issue.line is not None:
            location += f":{issue.line}"

        print(f"   Location: {location}")
        print(f"   {issue.description}")

        if issue.suggestion:
            print(f"   Suggestion: {issue.suggestion}")
        for evidence in issue.evidence:
            print(f"   Evidence: {evidence.repository}@{evidence.revision} "
                  f"{evidence.path}:{evidence.start_line}-{evidence.end_line}")


def main() -> int:
    load_dotenv()
    args = parse_args()
    client = None
    config = ReviewConfig(engine=args.engine, context_mode=args.context,
        **{key: getattr(args, key) for key in CONTEXT_BUDGET_OPTIONS})

    try:
        if args.github_pr is not None:
            client = GitHubClient()
            prepared = prepare_github_review(
                client, args.github_pr, config, expected_head_sha=args.expected_head_sha,
            )
            revision = prepared.revision
            source = f"GitHub PR #{args.github_pr}"
        else:
            prepared = prepare_local_review(config, paths=args.paths)
            source = "current Git changes"
            if args.paths is not None:
                source = 'selected local changes ' + json.dumps([item.path for item in prepared.files])

    except ReviewSupersededError as exc:
        print(f"Review skipped: {exc}")
        return 0

    except GitHubAPIError as exc:
        print(
            f"GitHub error: {exc}",
            file=sys.stderr,
        )
        return 1

    except Exception as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 1

    file_diffs, context = prepared.files, prepared.context
    if not file_diffs and client is None:
        print("No changes to review.")
        return 0

    print("=== Code Review Agent V4.2 ===")
    print(f"Review engine: {args.engine}")
    print(f"Context mode: {args.context}")
    if context is not None:
        print(f"Context revision: {context.revisions['head']}")
        limits = config.context_limits()
        print('Context budgets: ' + ', '.join(f'{key}={getattr(limits, key)}' for key in CONTEXT_BUDGET_OPTIONS))
    print(f"Reviewing {source}...")
    excluded_count = sum(not allowed_path(item.path) for item in file_diffs) if context is not None else 0
    candidate_count = len(file_diffs) - excluded_count
    print(f"Detected {len(file_diffs)} changed file(s); candidates: {candidate_count}; excluded: {excluded_count}.")
    if args.paths is not None:
        print(f"Selection covers {len(file_diffs)}/{prepared.detected_files} detected local changes; other changes are not reviewed.")
    max_file_requests = MAX_FILE_ROUNDS + MAX_FORMAT_REPAIRS
    if context is not None and candidate_count * max_file_requests > limits.max_model_calls:
        print(f"Budget note: {candidate_count} candidates can require up to {candidate_count * max_file_requests} model requests "
              f"at {MAX_FILE_ROUNDS} normal rounds plus {MAX_FORMAT_REPAIRS} format repair per file; configured limit is {limits.max_model_calls}. "
              "Use smaller local --paths batches or explicit budget overrides; other limits still apply.")
    if client is not None:
        print(f"Reviewed commit: {revision.head_sha}")
        print(f"Base commit: {revision.base_sha}")

    try:
        batch = execute_review(
            prepared,
            on_step=print_review_step if args.trace else None,
        )
        if client is not None and args.no_publish:
            ensure_current_revision(client, args.github_pr, revision)

    except Exception as exc:
        print(f"Review failed: {exc}", file=sys.stderr)
        return 1

    print_review(batch)

    publish_failed = False
    if args.github_pr is not None and not args.no_publish:
        try:
            action = post_github_comment(
                client,
                args.github_pr,
                batch,
                revision,
            )
            print(f"{action.capitalize()} review comment on GitHub PR.")

        except ReviewSupersededError as exc:
            print(f"Review skipped: {exc}")
            return 0

        except GitHubAPIError as exc:
            publish_failed = True
            print(
                f"Failed to post GitHub comment: {exc}",
                file=sys.stderr,
            )

    if args.no_publish:
        print('PR review only: no comment published.')

    for problem in batch.excluded:
        print(f"[EXCLUDED] {problem.path}: {problem.reason}")

    for problem in batch.failures:
        print(
            f"[FAILED] {problem.path}: {problem.reason}",
            file=sys.stderr,
        )

    for problem in batch.skipped:
        print(
            f"[SKIPPED] {problem.path}: {problem.reason}",
            file=sys.stderr,
        )

    if batch.incomplete:
        print(
            "Warning: review is incomplete.",
            file=sys.stderr,
        )
        return 1

    return 1 if publish_failed else 0

if __name__ == "__main__":
    raise SystemExit(main())
