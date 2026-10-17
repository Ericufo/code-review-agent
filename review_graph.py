"""V4.1: deterministic LangGraph orchestration around the existing reviewer.

The model still sees only a diff. Repository tools and model-directed routing
belong to V4.2; this graph establishes state, bounded iteration and progress.
"""

from collections.abc import Callable

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from diff_parser import FileDiff
from review_models import ReviewIssue, ReviewResult
from review_pipeline import (
    FileProblem,
    ReviewBatch,
    ReviewStep,
    finish_review_batch,
    get_skip_reason,
    review_file_issues,
)


class ReviewState(TypedDict, total=False):
    """Each node returns changed fields; default reducers replace those fields."""

    files: list[FileDiff]
    index: int
    active_path: str | None
    skip_reason: str | None
    attempted: int
    used_chars: int
    reviewed: int
    issues: list[ReviewIssue]
    failures: list[FileProblem]
    skipped: list[FileProblem]
    batch: ReviewBatch


def build_review_graph(
    *,
    review_fn: Callable[[str], ReviewResult],
    max_files: int,
    max_file_chars: int,
    max_total_chars: int,
):
    """Compile a graph with an injected model boundary for offline testing."""

    def check_file(state: ReviewState) -> dict:
        if state["index"] >= len(state["files"]):
            return {"active_path": None, "skip_reason": None}
        file_diff = state["files"][state["index"]]
        return {
            "active_path": file_diff.path,
            "skip_reason": get_skip_reason(
                file_diff, attempted=state["attempted"], used_chars=state["used_chars"],
                max_files=max_files, max_file_chars=max_file_chars,
                max_total_chars=max_total_chars,
            ),
        }

    def route_file(state: ReviewState) -> str:
        if state["index"] >= len(state["files"]):
            return "summarize"
        return "skip_file" if state["skip_reason"] is not None else "review_file"

    def skip_file(state: ReviewState) -> dict:
        file_diff = state["files"][state["index"]]
        reason = state["skip_reason"]
        assert reason is not None
        return {
            "index": state["index"] + 1,
            "skipped": [*state["skipped"], FileProblem(file_diff.path, reason)],
        }

    def review_file(state: ReviewState) -> dict:
        file_diff = state["files"][state["index"]]
        # Failed attempts also spend budget. No automatic retry in V4.1.
        update = {
            "index": state["index"] + 1,
            "attempted": state["attempted"] + 1,
            "used_chars": state["used_chars"] + len(file_diff.diff),
        }
        try:
            issues = review_file_issues(file_diff, review_fn)
        except Exception as exc:
            update["failures"] = [
                *state["failures"],
                FileProblem(file_diff.path, f"{type(exc).__name__}: {exc}"),
            ]
        else:
            update["issues"] = [*state["issues"], *issues]
            update["reviewed"] = state["reviewed"] + 1
        return update

    def summarize(state: ReviewState) -> dict:
        return {"batch": finish_review_batch(
            issues=state["issues"], failures=state["failures"], skipped=state["skipped"],
            total_files=len(state["files"]), reviewed_files=state["reviewed"],
        )}

    builder = StateGraph(ReviewState)
    builder.add_node("check_file", check_file)
    builder.add_node("review_file", review_file)
    builder.add_node("skip_file", skip_file)
    builder.add_node("summarize", summarize)
    builder.add_edge(START, "check_file")
    builder.add_conditional_edges("check_file", route_file, {
        "review_file": "review_file", "skip_file": "skip_file", "summarize": "summarize",
    })
    builder.add_edge("review_file", "check_file")
    builder.add_edge("skip_file", "check_file")
    builder.add_edge("summarize", END)
    return builder.compile()


def review_files_graph(
    file_diffs: list[FileDiff],
    *,
    review_fn: Callable[[str], ReviewResult],
    max_files: int,
    max_file_chars: int,
    max_total_chars: int,
    on_step: Callable[[ReviewStep], None] | None = None,
) -> ReviewBatch:
    graph = build_review_graph(
        review_fn=review_fn, max_files=max_files,
        max_file_chars=max_file_chars, max_total_chars=max_total_chars,
    )
    state: ReviewState = {
        "files": list(file_diffs), "index": 0, "active_path": None, "skip_reason": None,
        "attempted": 0, "used_chars": 0, "reviewed": 0,
        "issues": [], "failures": [], "skipped": [],
    }
    # Every file takes two nodes, then a final check and summary. Base this on
    # ALL input files, including skips, rather than the model-call budget.
    config = {"recursion_limit": 2 * len(file_diffs) + 4}
    for event in graph.stream(state, config=config, stream_mode="updates"):
        for node, update in event.items():
            state.update(update)
            if on_step is not None:
                on_step(ReviewStep(
                    node=node, path=state["active_path"], attempted=state["attempted"],
                    reviewed=state["reviewed"], failed=len(state["failures"]),
                    skipped=len(state["skipped"]), used_chars=state["used_chars"],
                ))
    return state["batch"]
