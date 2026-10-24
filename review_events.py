"""Small, source-free observations shared by the graph and evaluation runner."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from time import perf_counter
import warnings


def reason_code(message: str) -> str:
    """Classify existing diagnostics without persisting arbitrary exception text."""
    value = message.lower()
    if "time budget" in value or "timeouterror" in value:
        return "timeout"
    if "budget" in value or "maximum" in value or "diff exceeds" in value:
        return "budget_exhausted"
    if "invalid review" in value or "final answer messages" in value:
        return "invalid_output"
    if "model request failed" in value:
        return "model_error"
    if "repository context incomplete" in value or "tool failed" in value:
        return "tool_error"
    if "insufficient repository context" in value:
        return "insufficient_context"
    if "finding lacks evidence" in value:
        return "invalid_evidence"
    if "model response was not completed" in value or "unsupported model output" in value:
        return "invalid_output"
    if "excluded" in value:
        return "excluded"
    if "patch" in value or "binary" in value:
        return "unavailable_diff"
    return "review_error"


def response_usage(response) -> dict | None:
    """Copy only numeric public usage fields; never serialize raw responses."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    result = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        result[name] = value if type(value) is int and value >= 0 else None
    return result if any(value is not None for value in result.values()) else None


@dataclass(frozen=True)
class ReviewEvent:
    schema_version: int
    timestamp: str
    node: str
    path: str | None
    duration_ms: float
    outcome: str
    termination_reason: str | None
    counters: dict
    limits: dict
    api_usage: dict | None = None


def record_event(callback, *, node, started, outcome="ok", reason=None,
                 path=None, budget=None, counters=None, limits=None, usage=None):
    if callback is None:
        return
    counts, caps = {}, {}
    if budget is not None:
        for key, value in asdict(budget).items():
            (caps if key.startswith("max_") else counts)[key] = value
    counts.update(counters or {})
    caps.update(limits or {})
    event = ReviewEvent(
        schema_version=1, timestamp=datetime.now(timezone.utc).isoformat(),
        node=node, path=path, duration_ms=max(0.0, (perf_counter() - started) * 1000),
        outcome=outcome, termination_reason=reason,
        counters=counts, limits=caps, api_usage=usage,
    )
    # Observers must not turn a successful review into a failed file or expose
    # source-bearing callback exceptions. The runner uses an in-memory sink.
    try:
        callback(event)
    except Exception:
        warnings.warn("Review event observer failed; observation was dropped", RuntimeWarning)
