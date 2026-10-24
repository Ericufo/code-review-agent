"""Explicit, bounded DeepSeek evaluation. Never publishes comments.

The full provider context window is reserved before every request. Observed usage
settles that reservation at conservative peak/cache-miss rates, not billed cost.
An unknown charge retains the entire reservation and stops the run.
"""

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import importlib.metadata
import json
import os
from pathlib import Path
import platform
from time import perf_counter
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from evals.dataset import dataset_manifest, fingerprint, load_cases
from evals.runner import (ROOT, FILE_LIMITS, adjudication_template, code_version,
                         run_case, summarize)
from repository_context import ContextError
from review_events import response_usage
from review_models import ReviewResult
from review_tools import ModelBudgetError, TOOL_SCHEMAS
from reviewer import build_review_instructions


class LivePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    model: Literal["deepseek-flash"] = "deepseek-flash"
    base_url: Literal["https://api.deepseek.com"] = "https://api.deepseek.com"
    dataset_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    split: Literal["dev", "holdout"] = "dev"
    case_ids: list[str] = Field(default_factory=list)
    mode_order: list[Literal["diff-only", "context"]] = Field(default_factory=lambda: ["diff-only", "context"])
    holdout_config_frozen: bool = False
    annotation_review: Literal["ai_reviewed_user_accepted"] = "ai_reviewed_user_accepted"
    max_run_calls: int = Field(strict=True, gt=0, le=1000)
    max_cost_usd: Decimal = Field(gt=0, allow_inf_nan=False)
    max_case_calls: int = Field(default=6, strict=True, gt=0, le=7)
    max_input_chars: int = Field(default=200000, strict=True, gt=0)
    max_tool_calls: int = Field(default=24, strict=True, ge=0)
    max_context_chars: int = Field(default=96000, strict=True, ge=0)
    max_output_tokens: int = Field(default=4096, strict=True, gt=0, le=16384)
    timeout_seconds: int = Field(default=45, strict=True, gt=0, le=120)
    reasoning_effort: Literal["low"] = "low"
    # temperature is omitted: the provider ignores it in thinking mode.
    pricing_checked_on: date
    pricing_source: Literal["https://api-docs.deepseek.com/quick_start/pricing/"]
    input_usd_per_million: Decimal = Field(ge=Decimal("0.30"), allow_inf_nan=False)
    output_usd_per_million: Decimal = Field(ge=Decimal("1.20"), allow_inf_nan=False)
    # 2**20 also bounds a decimal 1M context; deliberately not a chars/token guess.
    provider_context_tokens: int = Field(default=1048576, strict=True, ge=1048576)

    @model_validator(mode="after")
    def consistent(self):
        if len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("Duplicate case IDs")
        if len(self.mode_order) != 2 or set(self.mode_order) != {"diff-only", "context"}:
            raise ValueError("Mode order must contain both comparison modes exactly once")
        if self.split == "holdout" and not self.holdout_config_frozen:
            raise ValueError("Holdout requires a frozen configuration")
        if self.max_cost_usd < self.reservation:
            raise ValueError("Cost budget cannot reserve one worst-case request")
        return self

    @property
    def reservation(self):
        return (self.provider_context_tokens * self.input_usd_per_million
                + self.max_output_tokens * self.output_usd_per_million) / Decimal(1000000)

    def context_limits(self):
        return {"max_model_calls": self.max_case_calls, "max_input_chars": self.max_input_chars,
                "max_tool_calls": self.max_tool_calls, "max_context_chars": self.max_context_chars}


def selected_cases(plan):
    cases = load_cases()
    if dataset_manifest(cases)["dataset_hash"] != plan.dataset_hash:
        raise ValueError("Plan does not match the frozen dataset")
    acceptance = json.loads((ROOT / "evals/annotation-review.json").read_text(encoding="utf-8"))
    if acceptance["dataset_hash"] != plan.dataset_hash or acceptance["status"] != plan.annotation_review:
        raise ValueError("Annotation review decision does not match this dataset")
    selected = [c for c in cases if c.split == plan.split and (not plan.case_ids or c.case_id in plan.case_ids)]
    if not selected or (plan.case_ids and len(selected) != len(plan.case_ids)):
        raise ValueError("Case selection is empty or contains IDs outside the split")
    return selected


class Ledger:
    """Sequential transport ledger. No reservation is released on unknown usage."""
    def __init__(self, plan, sink):
        self.plan, self.sink = plan, sink
        self.calls = 0
        self.charged = Decimal(0)
        self.stop_reason = None
        self.bound_valid = True

    def reserve(self):
        if self.stop_reason:
            raise ModelBudgetError("Run budget stopped: " + self.stop_reason)
        if self.calls >= self.plan.max_run_calls:
            self.stop_reason = "run_call_budget"
        elif self.charged + self.plan.reservation > self.plan.max_cost_usd:
            self.stop_reason = "run_cost_budget"
        if self.stop_reason:
            raise ModelBudgetError("Run budget exhausted: " + self.stop_reason)
        self.calls += 1
        self.charged += self.plan.reservation

    def settle(self, usage):
        if (not usage or any(usage.get(k) is None for k in ("input_tokens", "output_tokens"))):
            self.stop_reason = "usage_unavailable"
            return None
        if (usage["input_tokens"] > self.plan.provider_context_tokens
                or usage["output_tokens"] > self.plan.max_output_tokens):
            self.stop_reason = "provider_bound_violation"
            self.bound_valid = False
            return None
        cost = (usage["input_tokens"] * self.plan.input_usd_per_million
                + usage["output_tokens"] * self.plan.output_usd_per_million) / Decimal(1000000)
        self.charged += cost - self.plan.reservation
        return cost

    def snapshot(self):
        return {"model_calls": self.calls, "cost_upper_bound_usd": str(self.charged),
                "cost": None, "currency": "USD", "stop_reason": self.stop_reason,
                "bound_valid": self.bound_valid}


class LiveModel:
    """Source-only model adapter; does not receive labels, gold or case objects."""
    def __init__(self, plan, ledger, create, record_id):
        self.plan, self.ledger, self.create = plan, ledger, create
        self.record_id = record_id
        self.requests = []
        self.input_chars = 0

    def context_request(self, inputs, instructions, *, tools_enabled=True):
        return self.request(inputs, instructions, tools_enabled=tools_enabled)

    def request(self, inputs, instructions, *, tools_enabled):
        payload = {"model": self.plan.model, "input": inputs, "instructions": instructions,
                   "tools": TOOL_SCHEMAS if tools_enabled else [],
                   "max_output_tokens": self.plan.max_output_tokens,
                   "reasoning": {"effort": self.plan.reasoning_effort},
                   "text": {"format": {"type": "json_object"}}}
        size = len(json.dumps(payload, ensure_ascii=False))
        if len(self.requests) >= self.plan.max_case_calls or self.input_chars + size > self.plan.max_input_chars:
            raise ModelBudgetError("Case transport budget exhausted")
        self.ledger.reserve()
        self.input_chars += size
        entry = {"node": "transport_request", "record_id": self.record_id,
                 "request_number": self.ledger.calls, "input_chars": size,
                 "request_hash": fingerprint(payload), "api_usage": None,
                 "outcome": "in_flight", "termination_reason": None,
                 "cost_upper_bound_usd": str(self.plan.reservation), "cost": None,
                 "timestamp": datetime.now(timezone.utc).isoformat()}
        self.requests.append(entry)
        # Durable reservation precedes dispatch. Disk failure prevents the API call.
        try:
            self.ledger.sink(dict(entry))
        except BaseException as exc:
            # We know create() has not been called. Do not count this as dispatch.
            self.ledger.calls -= 1
            self.ledger.charged -= self.plan.reservation
            self.input_chars -= size
            self.requests.pop()
            self.ledger.stop_reason = "interrupted" if isinstance(exc, KeyboardInterrupt) else "recording_error"
            raise
        start = perf_counter()
        try:
            response = self.create(**payload)
            entry["api_usage"] = response_usage(response)
            provider_model = getattr(response, "model", None)
            entry["provider_model"] = (provider_model if isinstance(provider_model, str)
                                       and len(provider_model) <= 100
                                       and all(ch.isalnum() or ch in "-._" for ch in provider_model) else None)
            settled = self.ledger.settle(entry["api_usage"])
            entry.update(outcome="returned", termination_reason=self.ledger.stop_reason)
            if settled is not None:
                entry["cost_upper_bound_usd"] = str(settled)
        except BaseException as exc:
            self.ledger.stop_reason = "interrupted" if isinstance(exc, KeyboardInterrupt) else "usage_unavailable"
            entry.update(outcome="error", termination_reason=self.ledger.stop_reason)
            if not isinstance(exc, Exception):
                raise
            # Do not retain API exception bodies, request contents or credentials.
            raise ContextError("Model request failed (transport_error)") from None
        finally:
            entry["duration_ms"] = (perf_counter() - start) * 1000
            try:
                self.ledger.sink({**entry, "node": "transport_result"})
            except Exception:
                self.ledger.stop_reason = "recording_error"
                raise
        return response

    def diff_review(self, diff):
        from context_review import final_answer_text
        inputs = [{"role": "user", "content": f"<git_diff>\n{diff}\n</git_diff>"}]
        instructions = build_review_instructions()
        initial_calls = len(self.requests)
        # One bounded format repair in each mode; no network retry in either.
        for attempt in range(2):
            try:
                response = self.request(inputs, instructions, tools_enabled=False)
            except ModelBudgetError:
                if len(self.requests) > initial_calls:
                    raise ContextError("File model budget exhausted after an attempted review") from None
                raise
            try:
                if response.status != "completed":
                    raise ContextError("Model response was not completed")
                output = [item.model_dump(exclude_none=True) for item in response.output]
                if any(item.get("type") not in ("message", "reasoning") for item in output):
                    raise ContextError("Unsupported model output item")
                return ReviewResult.model_validate_json(final_answer_text(response, output))
            except (ValidationError, ContextError):
                if attempt:
                    raise ContextError("Model returned invalid review JSON") from None
                inputs.append({"role": "user", "content": "Return one complete JSON object matching the schema. Do not call tools."})

    def resources(self):
        usage = {}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            values = [(r["api_usage"] or {}).get(key) for r in self.requests]
            usage[key] = sum(values) if values and all(v is not None for v in values) else None
        return {"model_calls": len(self.requests), "input_chars": self.input_chars,
                "api_usage": usage if any(v is not None for v in usage.values()) else None,
                "usage_observed_requests": sum(r["api_usage"] is not None for r in self.requests),
                "usage_missing_requests": sum(r["api_usage"] is None for r in self.requests),
                "cost_upper_bound_usd": str(sum((Decimal(r["cost_upper_bound_usd"]) for r in self.requests), Decimal(0))),
                "cost": None, "currency": "USD", "measurement": "transport_dispatch",
                "bound_valid": self.ledger.bound_valid}


def write_json(path, data):
    staging = path.with_suffix(path.suffix + ".tmp")
    staging.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    staging.replace(path)


def pending_result(case, mode, run_id, version, plan):
    return {"record_id": f"{case.case_id}/{mode}/success", "run_id": run_id,
            "case_id": case.case_id, "case_hash": case.case_hash, "split": case.split,
            "label": case.label, "mode": mode, "suite": "cases", "scenario": "success",
            "execution_kind": "live", "execution_status": "not_run", "status": None,
            "termination_reason": "not_started", "code_version": version, "model": plan.model,
            "versions": case.review_inputs()[1].revisions, "changed_files": case.changed_files,
            "eligible_files": len(case.changed_files), "total_files": len(case.changed_files),
            "reviewed_files": 0, "expected_defects": len(case.expectation.defects),
            "findings": [], "failures": [], "skipped": [], "excluded": [],
            "files": [{"path": p, "status": "unknown", "reason": "not_started"} for p in case.changed_files],
            "duration_ms": 0, "resources": {"model_calls": 0, "tool_attempts": 0, "cost": None},
            "adjudication_status": "pending"}


def run_live(plan, *, output, create):
    """Injected transport for tests. CLI is the sole credential-bearing entry."""
    cases = selected_cases(plan)
    version, run_id = code_version(), str(uuid4())
    manifest = {"schema_version": 1, "rubric_version": 1, "run_id": run_id,
                "execution_kind": "live", "suite": "cases", "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(), "code_version": version,
                "python": platform.python_version(),
                "dependencies": {name: importlib.metadata.version(name) for name in ("langgraph", "pydantic", "openai")},
                "plan": plan.model_dump(mode="json"), "plan_hash": fingerprint(plan.model_dump(mode="json")),
                "annotation_review_hash": fingerprint(json.loads((ROOT / "evals/annotation-review.json").read_text(encoding="utf-8"))),
                "selection_hash": dataset_manifest(cases)["dataset_hash"],
                "prompt_version": fingerprint({name: (ROOT / name).read_text(encoding="utf-8")
                    for name in ("reviewer.py", "context_review.py", "review_models.py", "review_tools.py", "evals/live.py")}),
                "cases": [{"case_id": c.case_id, "case_hash": c.case_hash, "split": c.split} for c in cases],
                "order": "case_id ascending; " + " then ".join(plan.mode_order) + "; one attempt per record"}
    manifest["transport_policy"] = {"sdk_max_retries": 0, "max_format_repairs_per_file": 1,
                                     "reservation": "full provider context plus max output at peak rates"}
    shared = {key: value for key, value in plan.model_dump(mode="json").items()
              if key in ("model", "base_url", "mode_order", "max_case_calls", "max_input_chars", "max_tool_calls",
                         "max_context_chars", "max_output_tokens", "timeout_seconds", "reasoning_effort")}
    manifest["comparison_config_hash"] = fingerprint({"settings": shared, "prompt_version": manifest["prompt_version"]})
    output.mkdir(parents=True, exist_ok=False)
    sequence = 0

    def sink(entry):
        nonlocal sequence
        sequence += 1
        with (output / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({**entry, "run_id": run_id, "stream_sequence": sequence}, ensure_ascii=False) + "\n")
            stream.flush()

    ledger = Ledger(plan, sink)
    schedule = [(case, mode) for case in cases for mode in plan.mode_order]
    rows = [pending_result(c, m, run_id, version, plan) for c, m in schedule]

    def checkpoint():
        manifest["resources"] = ledger.snapshot()
        artifact = {"manifest": manifest, "results": rows, "summary": summarize(rows)}
        write_json(output / "manifest.json", manifest)
        write_json(output / "results.json", artifact)
        write_json(output / "adjudications.json", adjudication_template(rows))
        lines = ["# Live evaluation execution report", "", f"Run: `{run_id}`", "",
                 f"Run status: {manifest['status']}; stop reason: {ledger.stop_reason}",
                 f"Dispatched requests: {ledger.calls}; reserved/settled cost upper bound (USD): {ledger.charged}",
                 "Billed cost is unknown. Quality metrics await adjudication; this is not a scored baseline.",
                 "Annotation review: AI reviewed, user accepted; independent human gold review waived.", "",
                 "| Record | Execution | Review | Reviewed / eligible | Calls | Elapsed ms | Reason |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in rows:
            lines.append(f"| {r['record_id']} | {r['execution_status']} | {r['status']} | {r['reviewed_files']}/{r['eligible_files']} | {r['resources']['model_calls']} | {r['duration_ms']:.2f} | {r['termination_reason']} |")
        (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return artifact

    checkpoint()
    try:
        for index, (case, mode) in enumerate(schedule):
            if ledger.stop_reason:
                break
            rows[index]["execution_status"] = "running"
            checkpoint()
            model = LiveModel(plan, ledger, create, rows[index]["record_id"])
            rows[index] = run_case(case, run_id=run_id, mode=mode, model=model,
                limits=FILE_LIMITS, context_limits=plan.context_limits(), version=version,
                execution_kind="live", model_name=plan.model, event_sink=sink)
            if rows[index]["execution_status"] == "interrupted":
                ledger.stop_reason = "interrupted"
            checkpoint()
    except KeyboardInterrupt:
        ledger.stop_reason = "interrupted"
    except Exception:
        ledger.stop_reason = ledger.stop_reason or "runner_error"
        raise
    finally:
        manifest["status"] = "stopped" if ledger.stop_reason else "finished"
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        for row in rows:
            if row["execution_status"] in ("not_run", "running"):
                row["termination_reason"] = ledger.stop_reason or "runner_error"
                if row["execution_status"] == "running":
                    row["execution_status"] = "interrupted" if ledger.stop_reason == "interrupted" else "failed"
                for file in row["files"]:
                    file["reason"] = row["termination_reason"]
        artifact = checkpoint()
    return artifact


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate an evaluation plan offline, or explicitly run bounded live reviews.")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="Spend API calls; otherwise only validate the plan")
    parser.add_argument("--output", type=Path, help="New output directory; no overwrite or automatic resume")
    args = parser.parse_args(argv)
    try:
        plan = LivePlan.model_validate_json(args.plan.read_text(encoding="utf-8"))
        cases = selected_cases(plan)
    except (OSError, ValueError, KeyError):
        parser.exit(2, "Invalid evaluation plan or dataset; check required fields, ranges and frozen hash.\n")
    if not args.live:
        print(json.dumps({"validation": "passed", "live": False, "records": 2 * len(cases),
                          "reservation_per_request_usd": str(plan.reservation), "plan_hash": fingerprint(plan.model_dump(mode="json"))}, indent=2))
        return 0
    if plan.pricing_checked_on != datetime.now(timezone.utc).date():
        parser.exit(2, "Verify provider prices/context limit today (UTC) and update pricing_checked_on before --live.\n")
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        parser.exit(2, "DEEPSEEK_API_KEY is not set; no request was sent.\n")
    output = args.output or ROOT / "evals" / "runs" / str(uuid4())
    try:
        from openai import OpenAI
        with OpenAI(api_key=key, base_url=plan.base_url, timeout=plan.timeout_seconds, max_retries=0) as client:
            artifact = run_live(plan, output=output, create=client.responses.create)
    except (OSError, ValueError):
        parser.exit(2, "Cannot create/write the run directory; use a new writable output path.\n")
    print(json.dumps({"run_id": artifact["manifest"]["run_id"], "execution_kind": "live",
                      "status": artifact["manifest"]["status"], "output": str(output.resolve()),
                      "resources": artifact["manifest"]["resources"]}, indent=2))
    return 0 if artifact["manifest"]["status"] == "finished" and all(r["status"] == "COMPLETE" for r in artifact["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
