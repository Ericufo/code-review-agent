"""Run fixture reviews offline and export observations plus an unscored report."""

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
from time import perf_counter
from uuid import uuid4

from evals.dataset import CASES_DIR, EvalCase, dataset_manifest, fingerprint, load_cases
from evals.mocks import ScriptedModel
from review_events import reason_code, record_event
from review_graph import review_files_graph
from review_tools import ContextBudget


ROOT = Path(__file__).resolve().parents[1]
MODEL_NODES = {"model_request", "finalize_request", "repair_request"}
SCENARIOS = ("invalid_output", "tool_error", "budget_exhausted", "partial", "model_error")
FILE_LIMITS = {"max_files": 30, "max_file_chars": 20_000, "max_total_chars": 160_000}


def code_version():
    paths = sorted([*ROOT.glob("*.py"), *Path(__file__).parent.glob("*.py")])
    source_hash = fingerprint({p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in paths})
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10)
    return {"git_head": result.stdout.strip() if result.returncode == 0 else None,
            "source_hash": source_hash}


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def resource_summary(events):
    requests = [event for event in events if event["node"] in MODEL_NODES]
    totals = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        values = [(event["api_usage"] or {}).get(key) for event in requests]
        totals[key] = sum(values) if values and all(value is not None for value in values) else None
    usage = totals if any(value is not None for value in totals.values()) else None
    return {"model_calls": len(requests),
            "tool_attempts": sum(event["node"] in ("read_file", "search_code", "unknown_tool") for event in events),
            "api_usage": usage,
            "usage_observed_requests": sum(event["api_usage"] is not None for event in requests),
            "usage_missing_requests": sum(event["api_usage"] is None for event in requests),
            "cost": None}


def partial_case(case):
    data = case.model_dump()
    data["fixture"]["head"]["zz_failure.py"] = "probe_value = 1\n"
    data["changed_files"].append("zz_failure.py")
    return EvalCase.model_validate(data)


def run_case(case, *, run_id, mode, scenario="success", model=None,
             limits=None, context_limits=None, events=None, version=None,
             execution_kind="mock", model_name="scripted-v2", event_sink=None):
    """Inject model and repository doubles while using the production graph."""
    if mode not in ("context", "diff-only"):
        raise ValueError("Unsupported review mode")
    if scenario not in ("success", *SCENARIOS):
        raise ValueError("Unsupported reliability scenario")
    if mode != "context" and scenario != "success":
        raise ValueError("Reliability scenarios currently exercise the context graph")
    source_case_hash = case.case_hash
    if scenario == "partial":
        case = partial_case(case)
    diffs, context = case.review_inputs()
    configured_limits = {**FILE_LIMITS, **(limits or {})}
    budget = ContextBudget(**(context_limits or {}))
    if scenario == "budget_exhausted":
        budget.max_model_calls = 0
    configured_context_limits = {key: value for key, value in asdict(budget).items() if key.startswith("max_")}
    if scenario == "tool_error":
        def failed_read(path, version):
            raise OSError("Injected repository read failure")
        context.read = failed_read
    model = model or ScriptedModel(scenario)
    record_id = f"{case.case_id}/{mode}/{scenario}"
    records = []
    started = perf_counter()

    def collect(event):
        entry = {**asdict(event), "run_id": run_id, "record_id": record_id,
                        "case_id": case.case_id, "case_hash": case.case_hash,
                        "mode": mode, "scenario": scenario,
                        "versions": context.revisions, "code_version": version,
                        "sequence": len(records) + 1, "elapsed_ms": (perf_counter() - started) * 1000}
        records.append(entry)
        if event_sink is not None:
            event_sink(entry)

    result = {
        "record_id": record_id, "run_id": run_id, "case_id": case.case_id,
        "case_hash": case.case_hash, "source_case_hash": source_case_hash,
        "fixture_version": case.fixture_version, "schema_version": 1, "rubric_version": 1,
        "split": case.split, "label": case.label, "mode": mode, "scenario": scenario,
        "suite": "cases" if scenario == "success" else "reliability", "execution_kind": execution_kind,
        "versions": context.revisions, "changed_files": case.changed_files,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": model_name, "code_version": version,
        "limits": {**configured_limits, **(configured_context_limits if mode == "context" else {})},
        "execution_status": "failed", "status": None, "termination_reason": None,
        "total_files": len(diffs), "eligible_files": len(diffs), "reviewed_files": 0,
        "failures": [], "skipped": [], "excluded": [], "findings": [], "files": [],
        "expected_defects": len(case.expectation.defects),
        "adjudication_status": "pending", "quality_metrics": {
            "precision": None, "recall": None, "evidence_valid_rate": None},
    }
    try:
        batch = review_files_graph(diffs, review_fn=model.diff_review, **configured_limits,
            context=context if mode == "context" else None,
            context_request_fn=model.context_request if mode == "context" else None,
            on_record=collect, **(configured_context_limits if mode == "context" else {}))
    except KeyboardInterrupt:
        if execution_kind != "live":
            raise
        result.update(execution_status="interrupted", termination_reason="interrupted")
    except Exception as exc:
        result["termination_reason"] = reason_code(f"{type(exc).__name__}: {exc}")
    else:
        result.update(execution_status="finished", status=batch.status, termination_reason=batch.status,
                      total_files=batch.total_files, eligible_files=batch.eligible_files,
                      reviewed_files=batch.reviewed_files)
        for name in ("failures", "skipped", "excluded"):
            result[name] = [{"path": item.path, "reason": reason_code(item.reason)} for item in getattr(batch, name)]
        result["findings"] = [{"finding_id": f"f{index}", **issue.model_dump()}
                              for index, issue in enumerate(batch.result.issues, 1)]
    terminals = {event["path"]: event for event in records
                 if event["node"] in ("review_file", "skip_file", "exclude_file")}
    for path in case.changed_files:
        event = terminals.get(path)
        result["files"].append({"path": path, "status": event["outcome"] if event else "unknown",
                                "reason": event["termination_reason"] if event else result["termination_reason"]})
    result["duration_ms"] = (perf_counter() - started) * 1000
    result["resources"] = resource_summary(records)
    if execution_kind == "live":
        # Transport dispatches, rather than graph attempts, are authoritative.
        result["resources"].update(model.resources())
    result["counters"] = records[-1]["counters"] if records else {}
    record_event(collect, node="case_end", started=started,
                 outcome=result["execution_status"], reason=result["termination_reason"],
                 counters=result["counters"], limits=result["limits"])
    if events is not None:
        events.extend(records)
    return result


def adjudication_template(results):
    return [{"run_id": r["run_id"], "record_id": r["record_id"], "case_id": r["case_id"], "case_hash": r["case_hash"],
             "status": "pending", "reviewer": None, "date": None, "rubric_version": 1,
             "findings": [{"finding_id": f["finding_id"], "duplicate_of": None, "defect_id": None,
                           "verdict": "pending", "evidence_valid": None, "reason": None} for f in r["findings"]]}
            for r in results if r["suite"] == "cases"]


def summarize(results):
    """Group modes/splits/suites; do not turn mock findings into quality scores."""
    groups = []
    keys = sorted({(r["suite"], r["mode"], r["split"]) for r in results})
    for suite, mode, split in keys:
        rows = [r for r in results if (r["suite"], r["mode"], r["split"]) == (suite, mode, split)]
        complete = [r for r in rows if r["status"] == "COMPLETE" and r["eligible_files"] > 0]
        eligible, reviewed = sum(r["eligible_files"] for r in rows), sum(r["reviewed_files"] for r in rows)
        groups.append({
            "suite": suite, "mode": mode, "split": split, "case_count": len(rows),
            "status_counts": dict(Counter(r["status"] or "EXECUTION_FAILED" for r in rows)),
            "eligible_files": eligible, "reviewed_files": reviewed,
            "file_coverage": ratio(reviewed, eligible), "complete_cases": len(complete),
            "case_completion_rate": ratio(len(complete), len(rows)),
            "all_cases": {"cases": len(rows), "gold_defects": sum(r["expected_defects"] for r in rows),
                          "raw_findings": sum(len(r["findings"]) for r in rows),
                          "precision": None, "recall": None, "evidence_valid_rate": None},
            "completed_subset": {"cases": len(complete), "gold_defects": sum(r["expected_defects"] for r in complete),
                                 "raw_findings": sum(len(r["findings"]) for r in complete),
                                 "precision": None, "recall": None, "evidence_valid_rate": None},
            "normal_complete_zero_findings": sum(r["label"] == "clean" and not r["findings"] for r in complete),
            "normal_unknown": sum(r["label"] == "clean" and r not in complete for r in rows),
            "model_calls": sum(r["resources"]["model_calls"] for r in rows),
            "tool_attempts": sum(r["resources"]["tool_attempts"] for r in rows),
        })
    return groups


def check_execution(result):
    """Check injected control-flow expectations, never infer model accuracy."""
    scenario = result["scenario"]
    if result["execution_status"] != "finished":
        return False
    if scenario == "success":
        return result["status"] == "COMPLETE" and result["reviewed_files"] == result["eligible_files"] > 0
    expected = {
        "invalid_output": (0, 1, 0, "invalid_output"),
        "tool_error": (0, 1, 0, "tool_error"),
        "budget_exhausted": (0, 0, 1, "budget_exhausted"),
        "partial": (1, 1, 0, "model_error"),
        "model_error": (0, 1, 0, "model_error"),
    }[scenario]
    problems = result["failures"] + result["skipped"]
    observed = (result["reviewed_files"], len(result["failures"]), len(result["skipped"]),
                problems[0]["reason"] if problems else None)
    return (result["status"] == "INCOMPLETE" and observed == expected
            and (scenario != "partial" or len(result["findings"]) == 1))


def report_markdown(manifest, results, groups):
    lines = ["# Offline evaluation pipeline report", "",
             f"Run: `{manifest['run_id']}`", "",
             "Scripted mock execution only. This is not a model-quality baseline.",
             f"Control-flow checks: {sum(check_execution(result) for result in results)}/{len(results)} passed.",
             "Precision, recall, deduplication and semantic evidence validity are N/A pending human adjudication.",
             "API usage and cost are null when unavailable; character counts are not tokens.", "",
             "| Suite | Mode | Split | Complete cases | Reviewed / eligible | Model calls | Tool attempts |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for group in groups:
        lines.append(f"| {group['suite']} | {group['mode']} | {group['split']} | {group['complete_cases']}/{group['case_count']} | "
                     f"{group['reviewed_files']}/{group['eligible_files']} | {group['model_calls']} | {group['tool_attempts']} |")
    lines += ["", "## Per-case results", "",
              "| Record | Status | Reviewed / eligible | Raw findings | Elapsed ms |",
              "| --- | --- | --- | --- | --- |"]
    for result in results:
        lines.append(f"| {result['record_id']} | {result['status'] or 'EXECUTION_FAILED'} | "
                     f"{result['reviewed_files']}/{result['eligible_files']} | {len(result['findings'])} | {result['duration_ms']:.2f} |")
    lines += ["", "## Incomplete files", ""]
    incomplete = False
    for result in results:
        for file in result["files"]:
            if file["status"] not in ("ok", "excluded"):
                incomplete = True
                lines.append(f"- `{result['record_id']}`: `{file['path']}` — {file['status']}, {file['reason']}")
    if not incomplete:
        lines.append("None.")
    lines += ["", "## Interpretation", "",
              "- Reliability injections are separate from the fixture cases; their INCOMPLETE results are intentional.",
              "- Successful files and their findings survive failures in later files.",
              "- A clean case is only complete when its eligible files were actually reviewed; empty findings alone prove nothing.",
              "- results.json includes all-case and completed-subset denominators. Findings remain raw until adjudicated.",
              "- events.jsonl contains metadata/counters only, without diffs, tool source text, API bodies or reasoning.",
              "- Synthetic cases and deterministic mocks cannot establish general precision, recall or model improvements.",
              "- No live requests, retries outside the existing bounded repair, or GitHub publication are performed.", ""]
    return "\n".join(lines)


def run_dataset(cases, *, output, suite="all"):
    if suite not in ("all", "cases", "reliability"):
        raise ValueError("Unknown suite")
    if not cases:
        raise ValueError("No cases selected")
    by_id = {case.case_id: case for case in cases}
    if suite != "cases" and "shared-list-sort" not in by_id:
        raise ValueError("Reliability suite requires the shared-list-sort development fixture; use --suite cases")
    run_id = str(uuid4())
    version = code_version()
    manifest = {"schema_version": 1, "rubric_version": 1, "run_id": run_id,
                "started_at": datetime.now(timezone.utc).isoformat(), "execution_kind": "mock",
                "suite": suite, "code_version": version, "python": platform.python_version(),
                "selection_hash": dataset_manifest(cases)["dataset_hash"],
                "dependencies": {name: importlib.metadata.version(name) for name in ("langgraph", "pydantic", "openai")},
                "prompt_version": fingerprint({name: (ROOT / name).read_text(encoding="utf-8")
                                               for name in ("reviewer.py", "context_review.py", "review_models.py", "review_tools.py")}),
                "cases": [{"case_id": c.case_id, "case_hash": c.case_hash, "split": c.split} for c in cases]}
    # One new directory per run. Never overwrite or silently replace a failure.
    output.mkdir(parents=True, exist_ok=False)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    events, results = [], []
    if suite != "reliability":
        for case in cases:
            for mode in ("diff-only", "context"):
                results.append(run_case(case, run_id=run_id, mode=mode, events=events, version=version))
    if suite != "cases":
        for scenario in SCENARIOS:
            results.append(run_case(by_id["shared-list-sort"], run_id=run_id, mode="context",
                                    scenario=scenario, events=events, version=version))
    groups = summarize(results)
    checks = [{"record_id": result["record_id"], "passed": check_execution(result)} for result in results]
    artifact = {"manifest": manifest, "results": results, "summary": groups, "execution_checks": checks}
    (output / "results.json").write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    (output / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    (output / "report.md").write_text(report_markdown(manifest, results, groups), encoding="utf-8")
    adjudications = adjudication_template(results)
    (output / "adjudications.json").write_text(json.dumps(adjudications, indent=2), encoding="utf-8")
    return artifact


def main():
    parser = argparse.ArgumentParser(description="Run scripted offline reviews, never live APIs or publication.")
    parser.add_argument("--cases-dir", type=Path, default=CASES_DIR)
    parser.add_argument("--output", type=Path, help="New output directory; existing directories are rejected")
    parser.add_argument("--suite", choices=("all", "cases", "reliability"), default="all")
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    args = parser.parse_args()
    output = args.output or Path(__file__).parent / "runs" / str(uuid4())
    try:
        cases = [case for case in load_cases(args.cases_dir) if args.split == "all" or case.split == args.split]
        artifact = run_dataset(cases, output=output, suite=args.suite)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Cannot run offline evaluation: {exc}\n")
    print(json.dumps({"run_id": artifact["manifest"]["run_id"], "execution_kind": "mock",
                      "records": len(artifact["results"]),
                      "execution_checks_passed": all(check["passed"] for check in artifact["execution_checks"]),
                      "output": str(output.resolve())}, indent=2))
    # Expected INCOMPLETE is data. An unexpected control-flow result fails acceptance.
    return 0 if all(check["passed"] for check in artifact["execution_checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
