"""Offline fixture validation and model-input construction, without an API client."""

import argparse
import difflib
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from diff_parser import FileDiff
from repository_context import ContextError, RepositoryContext, validate_path


CASES_DIR = Path(__file__).parent / "cases"
DATASET_LOCK = CASES_DIR.parent / "dataset-lock.json"
Nonempty = Annotated[str, Field(min_length=1, pattern=r"\S")]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")]


class FixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Location(FixtureModel):
    version: Literal["base", "head"]
    path: Nonempty
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: Nonempty


class ExpectedDefect(FixtureModel):
    defect_id: Identifier
    category: Identifier
    primary_location: Location
    evidence: list[Location] = Field(min_length=1)
    claim: Nonempty
    trigger: Nonempty
    impact: Nonempty
    accepted_descriptions: list[Nonempty] = Field(min_length=1)
    rejected_descriptions: list[Nonempty] = Field(min_length=1)


class Expectation(FixtureModel):
    defects: list[ExpectedDefect]
    rationale: Nonempty
    evidence: list[Location] = Field(min_length=1)
    false_positive_traps: list[Nonempty]


class Snapshot(FixtureModel):
    base: dict[str, str]
    head: dict[str, str]


def fingerprint(value: object) -> str:
    """Canonical UTF-8 JSON digest, independent of file formatting/key order."""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EvalCase(FixtureModel):
    schema_version: Literal[1]
    fixture_version: int = Field(ge=1)
    case_id: Identifier
    scenario_id: Identifier
    split: Literal["dev", "holdout"]
    label: Literal["defect", "clean"]
    provenance: Nonempty
    changed_files: list[Nonempty] = Field(min_length=1)
    fixture: Snapshot
    expectation: Expectation

    @model_validator(mode="after")
    def validate_fixture(self):
        for sources in (self.fixture.base, self.fixture.head):
            for path, source in sources.items():
                try:
                    validate_path(path)
                except ContextError as exc:
                    raise ValueError(str(exc)) from exc
                # This format deliberately supports only LF-terminated text;
                # otherwise unified_diff would need no-newline marker handling.
                if "\r" in source or "\x00" in source or (source and not source.endswith("\n")):
                    raise ValueError("Fixture sources must be LF-terminated text (or empty)")
        paths = self.fixture.base.keys() | self.fixture.head.keys()
        changed = {path for path in paths
                   if self.fixture.base.get(path) != self.fixture.head.get(path)}
        if len(set(self.changed_files)) != len(self.changed_files) or set(self.changed_files) != changed:
            raise ValueError("changed_files must exactly match the snapshot changes, without duplicates")
        defects = self.expectation.defects
        if (self.label == "defect") != bool(defects):
            raise ValueError("defect cases need defects; clean cases require an explicit empty list")
        if len({item.defect_id for item in defects}) != len(defects):
            raise ValueError("Duplicate defect_id")
        locations = list(self.expectation.evidence)
        for defect in defects:
            if defect.primary_location.path not in changed:
                raise ValueError("Primary defect location must be in a changed file")
            locations.extend([defect.primary_location, *defect.evidence])
        for location in locations:
            source = getattr(self.fixture, location.version).get(location.path)
            if source is None:
                raise ValueError("Evidence path is absent from its declared version")
            lines = source.splitlines(keepends=True)
            if not location.start_line <= location.end_line <= len(lines):
                raise ValueError("Evidence line range is outside its source")
            quote = "".join(lines[location.start_line - 1:location.end_line])
            if quote != location.quote:
                raise ValueError("Evidence quote does not match its source lines")
        return self

    @property
    def case_hash(self) -> str:
        return fingerprint(self.model_dump())

    def review_inputs(self) -> tuple[list[FileDiff], RepositoryContext]:
        """Return only source/diffs, never labels, expected findings or rationale."""
        snapshots = self.fixture.model_dump()
        revisions = {version: fingerprint(sources) for version, sources in snapshots.items()}
        # Avoid encoding the answer (e.g. a case named *-bug) in model-visible IDs.
        repository = "fixture/" + fingerprint(snapshots).removeprefix("sha256:")
        context = RepositoryContext(repository, revisions, snapshots)
        diffs = []
        for path in self.changed_files:
            before, after = self.fixture.base.get(path), self.fixture.head.get(path)
            patch = "".join(difflib.unified_diff(
                (before or "").splitlines(keepends=True),
                (after or "").splitlines(keepends=True),
                fromfile=f"a/{path}" if before is not None else "/dev/null",
                tofile=f"b/{path}" if after is not None else "/dev/null",
            ))
            # Empty file additions/deletions still need an observable file change.
            mode = "new file mode 100644\n" if before is None else (
                "deleted file mode 100644\n" if after is None else "")
            diffs.append(FileDiff(path, f"diff --git a/{path} b/{path}\n{mode}{patch}"))
        return diffs, context


def dataset_manifest(cases: list[EvalCase]) -> dict:
    entries = [{"case_id": case.case_id, "case_hash": case.case_hash,
                "scenario_id": case.scenario_id, "split": case.split, "label": case.label}
               for case in sorted(cases, key=lambda case: case.case_id)]
    return {"schema_version": 1, "dataset_version": "v4.3-synthetic-20-v1",
            "dataset_hash": fingerprint(entries), "cases": entries}


def validate_dataset_lock(cases: list[EvalCase], lock_path: Path = DATASET_LOCK) -> None:
    stored = json.loads(lock_path.read_text(encoding="utf-8"))
    if stored != dataset_manifest(cases):
        raise ValueError("Dataset differs from its frozen manifest; review and version the dataset change")


def load_cases(directory: Path = CASES_DIR) -> list[EvalCase]:
    cases = [EvalCase.model_validate_json(path.read_text(encoding="utf-8"))
             for path in sorted(directory.glob("*.json"))]
    if not cases:
        raise ValueError(f"No evaluation cases in {directory}")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("Duplicate case_id")
    scenario_splits: dict[str, str] = {}
    for case in cases:
        previous = scenario_splits.setdefault(case.scenario_id, case.split)
        if previous != case.split:
            raise ValueError(f"Scenario leaks across splits: {case.scenario_id}")
    # Custom directories remain usable for validation tests and experiments.
    # The shipped catalogue must match its explicit, reviewed freeze artifact.
    if directory.resolve() == CASES_DIR.resolve():
        validate_dataset_lock(cases)
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate fixtures offline; does not run a review or score models.")
    parser.add_argument("--cases-dir", type=Path, default=CASES_DIR)
    args = parser.parse_args()
    try:
        cases = load_cases(args.cases_dir)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Invalid evaluation dataset: {exc}\n")
    print(json.dumps({
        "schema_version": 1,
        "validation": "passed",
        "case_count": len(cases),
        "target_case_count": 20,
        "dataset_hash": dataset_manifest(cases)["dataset_hash"],
        "lock_verified": args.cases_dir.resolve() == CASES_DIR.resolve(),
        "splits": {split: {label: sum(c.split == split and c.label == label for c in cases)
                           for label in ("defect", "clean")} for split in ("dev", "holdout")},
        "cases": [{"case_id": case.case_id, "case_hash": case.case_hash,
                   "changed_files": case.changed_files} for case in cases],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
