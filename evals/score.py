"""Validate explicit adjudications and score existing runs without model calls."""

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evals.dataset import load_cases, fingerprint
from evals.runner import ratio


class Mark(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    finding_id: str
    source_finding_id: str | None = None
    duplicate_of: str | None
    defect_id: str | None
    verdict: Literal["TP", "FP", "pending"]
    evidence_valid: bool | None
    reason: str | None


class Adjudication(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    record_id: str
    case_id: str
    case_hash: str
    status: Literal["pending", "completed"]
    reviewer: str | None
    date: str | None
    rubric_version: Literal[1]
    findings: list[Mark]


def unique(items, key):
    mapped = {getattr(item, key) if hasattr(item, key) else item[key]: item for item in items}
    if len(mapped) != len(items):
        raise ValueError("Duplicate " + key)
    return mapped


def score(artifact, annotations, *, allow_mock=False):
    manifest = artifact["manifest"]
    if manifest["execution_kind"] != "live" and not allow_mock:
        raise ValueError("Mock scoring requires --allow-mock and is only a pipeline exercise")
    if manifest.get("rubric_version") != 1:
        raise ValueError("Unsupported rubric version")
    cases = {c.case_id: c for c in load_cases()}
    selection = unique(manifest["cases"], "case_id")
    for cid, selected in selection.items():
        if cid not in cases or selected["case_hash"] != cases[cid].case_hash or selected["split"] != cases[cid].split:
            raise ValueError("Run does not match the frozen dataset")
    rows = [r for r in artifact["results"] if r["suite"] == "cases"]
    by_record = unique(rows, "record_id")
    expected = {f"{cid}/{mode}/success" for cid in selection for mode in ("diff-only", "context")}
    if set(by_record) != expected:
        raise ValueError("Missing or unexpected scheduled records")
    marks = unique([Adjudication.model_validate(a) for a in annotations], "record_id")
    if set(marks) != set(by_record):
        raise ValueError("Adjudications must cover every scheduled case record")
    scored = []
    for rid, row in by_record.items():
        case = cases[row["case_id"]]
        mark = marks[rid]
        if rid != f"{case.case_id}/{row['mode']}/success" or row["mode"] not in ("diff-only", "context"):
            raise ValueError("Invalid record identity")
        for key, value in (("run_id", manifest["run_id"]), ("case_id", case.case_id), ("case_hash", case.case_hash)):
            if row[key] != value or getattr(mark, key) != value:
                raise ValueError("Stale or mismatched adjudication identity")
        if row["label"] != case.label or row["split"] != case.split or row["expected_defects"] != len(case.expectation.defects):
            raise ValueError("Run gold metadata differs from dataset")
        if row["execution_kind"] != manifest["execution_kind"]:
            raise ValueError("Cannot mix mock and live records")
        complete = row["status"] == "COMPLETE" and row["eligible_files"] > 0
        files = unique(row["files"], "path")
        if set(files) != set(case.changed_files):
            raise ValueError("Missing file completion records")
        if (row["eligible_files"] != len(case.changed_files)
                or row["reviewed_files"] != sum(f["status"] == "ok" for f in files.values())
                or (complete and (row["execution_status"] != "finished" or row["reviewed_files"] != row["eligible_files"]))):
            raise ValueError("Contradictory execution coverage")
        raw = unique(row["findings"], "finding_id")
        items = unique(mark.findings, "finding_id")
        sources = {item.finding_id: item.source_finding_id or item.finding_id for item in mark.findings}
        if any(cid in raw and cid != source for cid, source in sources.items()):
            raise ValueError("Claim ID cannot impersonate another raw finding")
        if set(sources.values()) != set(raw):
            raise ValueError("Missing or invented finding adjudications")
        for source in raw:
            claims = [i for i in mark.findings if sources[i.finding_id] == source]
            if len(claims) > 1 and any(i.source_finding_id != source or i.finding_id in raw for i in claims):
                raise ValueError("Split claims need distinct IDs and explicit source_finding_id")
        gold = {d.defect_id: d for d in case.expectation.defects}
        matched = set()
        verdicts = Counter()
        evidence_ok = evidence_missing = 0
        pending = mark.status != "completed"
        if mark.status == "completed":
            if not mark.reviewer or not mark.reviewer.strip() or not mark.date:
                raise ValueError("Completed adjudication needs reviewer and date")
            date.fromisoformat(mark.date)
        for item in mark.findings:
            finding = raw[sources[item.finding_id]]
            if finding["file"] not in files or files[finding["file"]]["status"] != "ok":
                raise ValueError("Finding is not from a successfully reviewed file")
            if item.duplicate_of is not None:
                representative = items.get(item.duplicate_of)
                if representative is None or representative.finding_id == item.finding_id or representative.duplicate_of is not None:
                    raise ValueError("Duplicate must point directly to a distinct representative in this record")
                if (item.verdict, item.defect_id, item.evidence_valid) != (representative.verdict, representative.defect_id, representative.evidence_valid):
                    raise ValueError("Duplicate classification must agree with its representative")
                if mark.status == "completed" and (not item.reason or not item.reason.strip()):
                    raise ValueError("Duplicate adjudication needs a reason")
                continue
            verdicts[item.verdict] += 1
            if item.verdict == "pending":
                pending = True
                if item.defect_id is not None:
                    raise ValueError("Pending findings cannot claim a gold match")
            else:
                if not item.reason or not item.reason.strip():
                    raise ValueError("Decided finding needs an evidence-based reason")
                if item.verdict == "TP":
                    if item.defect_id not in gold or item.defect_id in matched:
                        raise ValueError("TP must match one unique gold defect")
                    matched.add(item.defect_id)
                elif item.defect_id is not None:
                    raise ValueError("FP cannot claim a gold match")
                if row["mode"] == "context" and item.evidence_valid is None:
                    raise ValueError("Context findings require evidence adjudication")
            if row["mode"] == "diff-only" and item.evidence_valid is not None:
                raise ValueError("Diff-only citation metric must be N/A")
            if item.evidence_valid and not finding.get("evidence"):
                raise ValueError("Missing citations cannot be evidence-valid")
            evidence_ok += item.evidence_valid is True
            evidence_missing += not bool(finding.get("evidence"))
        if mark.status == "completed" and pending:
            raise ValueError("Completed adjudication cannot contain pending findings")
        fn = unobserved = 0
        for defect_id, defect in gold.items():
            if defect_id not in matched:
                if files[defect.primary_location.path]["status"] == "ok":
                    fn += 1
                else:
                    unobserved += 1
        scored.append({"record_id": rid, "mode": row["mode"], "split": case.split, "label": case.label,
                       "complete": complete, "pending": pending, "gold": len(gold),
                       "TP": verdicts["TP"], "FP": verdicts["FP"],
                       "FN": None if pending else fn, "unobserved": None if pending else unobserved,
                       "unique_findings": sum(verdicts.values()), "raw_findings": len(raw),
                       "evidence_valid": evidence_ok, "missing_citations": evidence_missing,
                       "TN": not pending and case.label == "clean" and complete and not raw,
                       "unknown": case.label == "clean" and (not complete or pending),
                       "eligible_files": row["eligible_files"], "reviewed_files": row["reviewed_files"]})

    def metrics(items, mode):
        totals = {k: sum(r[k] for r in items) for k in ("TP", "FP", "gold", "unique_findings", "raw_findings", "evidence_valid", "missing_citations", "TN", "unknown")}
        pending = any(r["pending"] for r in items)
        clean = [r for r in items if r["label"] == "clean" and r["complete"]]
        totals.update(cases=len(items), pending_records=sum(r["pending"] for r in items),
                      FN=None if pending else sum(r["FN"] for r in items),
                      unobserved=None if pending else sum(r["unobserved"] for r in items),
                      normal_fp_cases=sum(r["label"] == "clean" and r["FP"] > 0 for r in items),
                      normal_complete_cases=len(clean),
                      precision=None if pending else ratio(totals["TP"], totals["TP"] + totals["FP"]),
                      recall=None if pending else ratio(totals["TP"], totals["gold"]),
                      evidence_valid_rate=None if pending or mode == "diff-only" else ratio(totals["evidence_valid"], totals["unique_findings"]),
                      normal_false_positive_rate=None if pending else ratio(sum(r["FP"] > 0 for r in clean), len(clean)))
        return totals

    groups = []
    for mode, split in sorted({(r["mode"], r["split"]) for r in scored}):
        items = [r for r in scored if (r["mode"], r["split"]) == (mode, split)]
        complete = [r for r in items if r["complete"]]
        groups.append({"mode": mode, "split": split, "all_cases": metrics(items, mode),
                       "completed_subset": metrics(complete, mode),
                       "case_completion_rate": ratio(len(complete), len(items)),
                       "file_coverage": ratio(sum(r["reviewed_files"] for r in items), sum(r["eligible_files"] for r in items))})
    if any(r["pending"] for r in scored):
        for group in groups:
            for subset in ("all_cases", "completed_subset"):
                for metric in ("precision", "recall", "evidence_valid_rate", "normal_false_positive_rate"):
                    group[subset][metric] = None
    return {"schema_version": 1, "run_id": manifest["run_id"], "execution_kind": manifest["execution_kind"],
            "status": "pending" if any(r["pending"] for r in scored) else "adjudicated",
            "interpretation": "mock plumbing exercise" if manifest["execution_kind"] != "live" else "adjudicated synthetic evaluation",
            "results_hash": fingerprint(artifact), "adjudications_hash": fingerprint(annotations),
            "groups": groups, "records": scored}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--adjudications", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory, never overwrites raw results")
    parser.add_argument("--allow-mock", action="store_true", help="Exercise scoring; never a quality baseline")
    args = parser.parse_args(argv)
    try:
        artifact = json.loads((args.run / "results.json").read_text(encoding="utf-8"))
        annotations = json.loads(args.adjudications.read_text(encoding="utf-8"))
        result = score(artifact, annotations, allow_mock=args.allow_mock)
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "scores.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        lines = ["# Evaluation adjudication report", "", result["interpretation"],
                 f"Run: `{result['run_id']}`; adjudication: {result['status']}", "",
                 "All-case recall is known-defect detection rate, including unobserved defects in its denominator.",
                 "Pending judgments suppress final rates. Zero denominators are N/A. See scores.json for counts and provenance.", "",
                 "| Mode | Split | Subset | Cases | Gold | TP | FP | FN | Unobserved | Precision | Recall | Evidence valid |",
                 "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for group in result["groups"]:
            for subset in ("all_cases", "completed_subset"):
                m = group[subset]
                values = [m[k] for k in ("cases", "gold", "TP", "FP", "FN", "unobserved", "precision", "recall", "evidence_valid_rate")]
                lines.append("| " + " | ".join([group["mode"], group["split"], subset, *["N/A" if v is None else str(v) for v in values]]) + " |")
        (args.output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(2, "Invalid run/adjudications or output directory; no raw result was changed.\n")
    print(json.dumps({"run_id": result["run_id"], "status": result["status"], "output": str(args.output.resolve())}))
    return 0 if result["status"] == "adjudicated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
