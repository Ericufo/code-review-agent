"""Batch workflow with an optional V4.2 bounded model/tool subgraph."""

from collections.abc import Callable
from dataclasses import replace
from time import perf_counter

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from diff_parser import FileDiff
from review_models import ReviewIssue, ReviewResult
from review_events import reason_code, record_event
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
    excluded: list[FileProblem]
    exclude_reason: str | None
    batch: ReviewBatch


def build_review_graph(
    *,
    review_fn: Callable[[str], ReviewResult],
    max_files: int,
    max_file_chars: int,
    max_total_chars: int,
    context=None,
    context_request_fn=None,
    context_budget=None,
    on_step=None,
    on_record=None,
):
    """Compile a graph with an injected model boundary for offline testing."""

    if context is not None and context_budget is None:
        from review_tools import ContextBudget
        context_budget = ContextBudget()

    def check_file(state: ReviewState) -> dict:
        if state["index"] >= len(state["files"]):
            return {"active_path": None, "skip_reason": None}
        file_diff = state["files"][state["index"]]
        # Scope is decided before patch/size/budget checks. Exclusions are not
        # unfinished work, even when an excluded artifact is huge or has no patch.
        if context is not None:
            from repository_context import allowed_path
            if not allowed_path(file_diff.path):
                return {"active_path": file_diff.path, "skip_reason": None,
                        "exclude_reason": "file excluded by repository source policy"}
        reason = get_skip_reason(
            file_diff, attempted=state["attempted"], used_chars=state["used_chars"],
            max_files=max_files, max_file_chars=max_file_chars,
            max_total_chars=max_total_chars,
        )
        if context is not None and reason is None:
            if context_budget.model_calls >= context_budget.max_model_calls:
                reason = "batch model call budget exhausted"
        return {
            "active_path": file_diff.path,
            "skip_reason": reason,
            "exclude_reason": None,
        }

    def route_file(state: ReviewState) -> str:
        if state["index"] >= len(state["files"]):
            return "summarize"
        if state.get("exclude_reason") is not None:
            return "exclude_file"
        return "skip_file" if state["skip_reason"] is not None else "review_file"

    def exclude_file(state: ReviewState) -> dict:
        return {"index": state["index"] + 1,
                "excluded": [*state.get('excluded', []),
                    FileProblem(state['active_path'], state['exclude_reason'])]}

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
        initial_calls = context_budget.model_calls if context is not None else 0
        try:
            if context is None:
                started = perf_counter()
                reason = None
                request_rejected = False
                try:
                    issues = review_file_issues(file_diff, review_fn)
                except Exception as exc:
                    from review_tools import ModelBudgetError
                    request_rejected = isinstance(exc, ModelBudgetError)
                    reason = reason_code(f'{type(exc).__name__}: {exc}')
                    raise
                finally:
                    record_event(on_record, node='request_rejected' if request_rejected else 'model_request', path=file_diff.path,
                        started=started, outcome='error' if reason else 'ok', reason=reason,
                        counters={'model_calls': state['attempted'] if request_rejected else update['attempted'], 'tool_calls': 0,
                                  'input_chars': None, 'context_chars': 0},
                        limits={'max_files': max_files, 'max_file_chars': max_file_chars,
                                'max_total_chars': max_total_chars})
            else:
                from context_review import review_with_context

                def on_event(node):
                    if on_step is not None:
                        on_step(ReviewStep(
                            node=node, path=file_diff.path, attempted=update['attempted'],
                            reviewed=state['reviewed'], failed=len(state['failures']),
                            skipped=len(state['skipped']), used_chars=update['used_chars'],
                            model_calls=context_budget.model_calls,
                            tool_calls=context_budget.tool_calls,
                            context_chars=context_budget.context_chars,
                            input_chars=context_budget.input_chars,
                            model_limit=context_budget.max_model_calls,
                            tool_limit=context_budget.max_tool_calls,
                            context_limit=context_budget.max_context_chars,
                            input_limit=context_budget.max_input_chars,
                            excluded=len(state.get('excluded', [])),
                        ))

                issues = review_file_issues(file_diff, lambda diff: review_with_context(
                    diff, context=context, budget=context_budget,
                    request_fn=context_request_fn, on_event=on_event,
                    on_record=(lambda event: on_record(replace(event, path=file_diff.path)))
                              if on_record is not None else None,
                ), verified_context=True)
        except Exception as exc:
            from review_tools import ModelBudgetError
            if isinstance(exc, ModelBudgetError) and (context is None or context_budget.model_calls == initial_calls):
                # Rejected before this file's first request: it was not reviewed.
                update.update(attempted=state['attempted'], used_chars=state['used_chars'],
                    skipped=[*state['skipped'], FileProblem(file_diff.path, str(exc))])
                return update
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
            excluded=state.get('excluded', []),
        )}

    def observed(node, function):
        def run(state):
            started = perf_counter()
            update = function(state)
            merged = {**state, **update}
            outcome, reason = 'ok', None
            for field, status in (('failures', 'failed'), ('skipped', 'skipped'), ('excluded', 'excluded')):
                if len(merged.get(field, [])) > len(state.get(field, [])):
                    outcome = status
                    reason = reason_code(merged[field][-1].reason)
            if node == 'review_file' and outcome == 'ok':
                reason = 'completed'
            if node == 'summarize':
                reason = merged['batch'].status
            counters = {key: merged[key] for key in ('attempted', 'reviewed', 'used_chars')}
            counters.update({key: len(merged.get(key, [])) for key in ('failures', 'skipped', 'excluded')})
            if context is None:
                counters.update(model_calls=merged['attempted'], tool_calls=0, input_chars=None, context_chars=0)
            record_event(on_record, node=node, path=merged['active_path'], started=started,
                         outcome=outcome, reason=reason, budget=context_budget if context is not None else None,
                         counters=counters, limits={'max_files': max_files, 'max_file_chars': max_file_chars,
                                                   'max_total_chars': max_total_chars})
            return update
        return run

    builder = StateGraph(ReviewState)
    for node, function in (('check_file', check_file), ('review_file', review_file),
                           ('skip_file', skip_file), ('exclude_file', exclude_file), ('summarize', summarize)):
        builder.add_node(node, observed(node, function))
    builder.add_edge(START, "check_file")
    builder.add_conditional_edges("check_file", route_file, {
        "review_file": "review_file", "skip_file": "skip_file", "summarize": "summarize", "exclude_file": "exclude_file",
    })
    builder.add_edge("review_file", "check_file")
    builder.add_edge("skip_file", "check_file")
    builder.add_edge("exclude_file", "check_file")
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
    on_record=None,
    context=None,
    context_request_fn=None,
    max_model_calls: int | None = None,
    max_input_chars: int | None = None,
    max_context_chars: int | None = None,
    max_tool_calls: int | None = None,
) -> ReviewBatch:
    from review_tools import ContextBudget
    overrides = dict(max_model_calls=max_model_calls, max_input_chars=max_input_chars,
                     max_context_chars=max_context_chars, max_tool_calls=max_tool_calls)
    context_budget = ContextBudget(**{key: value for key, value in overrides.items() if value is not None})
    graph = build_review_graph(
        review_fn=review_fn, max_files=max_files,
        max_file_chars=max_file_chars, max_total_chars=max_total_chars,
        context=context, context_request_fn=context_request_fn,
        context_budget=context_budget, on_step=on_step, on_record=on_record,
    )
    state: ReviewState = {
        "files": list(file_diffs), "index": 0, "active_path": None, "skip_reason": None,
        "attempted": 0, "used_chars": 0, "reviewed": 0,
        "issues": [], "failures": [], "skipped": [], "excluded": [], "exclude_reason": None,
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
                    model_calls=context_budget.model_calls,
                    tool_calls=context_budget.tool_calls,
                    context_chars=context_budget.context_chars,
                    input_chars=context_budget.input_chars,
                    model_limit=context_budget.max_model_calls if context is not None else 0,
                    tool_limit=context_budget.max_tool_calls if context is not None else 0,
                    context_limit=context_budget.max_context_chars if context is not None else 0,
                    input_limit=context_budget.max_input_chars if context is not None else 0,
                    excluded=len(state.get('excluded', [])),
                ))
    return state["batch"]
