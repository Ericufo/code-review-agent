"""Scripted transport doubles. These verify plumbing, not model intelligence."""

import json
from types import SimpleNamespace

from diff_parser import parse_git_diff

from review_models import ReviewResult


class OutputItem:
    def __init__(self, value):
        self.value = value

    def model_dump(self, **kwargs):
        return dict(self.value)


def reply(*, calls=(), issues=(), text=None):
    return SimpleNamespace(
        status="completed", usage=None,
        output=[OutputItem(call) for call in calls],
        output_text=text if text is not None else json.dumps({
            "summary": "", "issues": list(issues), "context_complete": True, "context_reason": "",
        }),
    )


def scripted_issue(evidence=()):
    return {
        "severity": "medium", "file": "service.py", "line": 5,
        "title": "Sorting mutates the shared cache",
        "description": "values() returns the shared CACHE. Calling ranked_values sorts it in place, changing the order seen by later readers.",
        "suggestion": "Sort a copy of the returned list.", "evidence": list(evidence),
    }


class ScriptedModel:
    """Consumes only review inputs; no EvalCase, labels or expected defects."""

    def __init__(self, scenario="success"):
        self.scenario = scenario
        self.calls = 0

    def context_request(self, inputs, instructions, *, tools_enabled=True):
        self.calls += 1
        data = json.loads(inputs[0]["content"])
        diff = data["git_diff"]
        if self.scenario == "model_error" or (
                self.scenario == "partial" and "b/zz_failure.py\n" in diff):
            raise ConnectionError("Injected transport failure")
        if self.scenario == "invalid_output":
            return reply(text="not JSON")
        outputs = [json.loads(item["output"]) for item in inputs if item.get("type") == "function_call_output"]
        if not outputs and tools_enabled:
            sources = data["available_sources"]["head"]
            changed = {file.path for file in parse_git_diff(diff)}
            # Generic transport exercise, independent of labels and scenario IDs.
            # Prefer an unchanged dependency; do not implement gold-aware detectors.
            candidates = sorted(path for path in sources if path not in changed) or sorted(sources)
            if not candidates:
                return reply()
            dependency = candidates[0]
            return reply(calls=[{
                "type": "function_call", "call_id": f"read-{self.calls}", "name": "read_file",
                "arguments": json.dumps({"path": dependency, "start_line": 1, "end_line": 4}),
            }])
        # A tool error is intentionally followed by a clean answer: the real
        # context graph must reject it rather than hide the failed tool.
        evidence = [entry["evidence"] for output in outputs for entry in output.get("entries", [])
                    if "return CACHE" in entry["text"]]
        if "+    result.sort()" in diff and evidence:
            return reply(issues=[scripted_issue(evidence)])
        return reply()

    def diff_review(self, diff):
        self.calls += 1
        # Canned behavior is explicit; it is not a general diff-only detector.
        issues = [scripted_issue()] if "+    result.sort()" in diff else []
        return ReviewResult.model_validate({"summary": "", "issues": issues})
