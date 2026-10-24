import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evals.dataset import CASES_DIR, EvalCase, fingerprint, load_cases, validate_dataset_lock
from review_models import ReviewResult
from review_pipeline import review_files


class TestEvalDataset(unittest.TestCase):
    def setUp(self):
        self.cases = {case.case_id: case for case in load_cases()}
        self.bug = self.cases["shared-list-sort"]
        self.clean = self.cases["guaranteed-profile"]

    def test_representative_cases_have_explicit_gold_and_cross_file_evidence(self):
        self.assertEqual(len(self.cases), 20)
        self.assertEqual({case.label for case in self.cases.values()}, {"clean", "defect"})
        self.assertEqual({case.split for case in self.cases.values()}, {"dev", "holdout"})
        for split, count in (("dev", 6), ("holdout", 4)):
            for label in ("defect", "clean"):
                self.assertEqual(sum(c.split == split and c.label == label for c in self.cases.values()), count)
        self.assertEqual(len(self.bug.expectation.defects), 1)
        self.assertEqual(self.clean.expectation.defects, [])
        for case in self.cases.values():
            self.assertTrue(any(e.path not in case.changed_files for e in case.expectation.evidence))

    def test_inputs_work_with_existing_diff_parser_and_context_without_gold(self):
        from diff_parser import parse_git_diff
        for case in self.cases.values():
            diffs, context = case.review_inputs()
            self.assertEqual([item.path for item in diffs], case.changed_files)
            self.assertEqual(parse_git_diff("".join(item.diff for item in diffs)), diffs)
            for version in ("base", "head"):
                expected = getattr(case.fixture, version)
                self.assertEqual(context.paths(version), sorted(expected))
                for path, source in expected.items():
                    self.assertEqual(context.read(path, version), source)
                self.assertEqual(context.revisions[version], fingerprint(expected))
            serialized = json.dumps({"context": vars(context), "diffs": [vars(d) for d in diffs]})
            self.assertNotIn(case.case_id, serialized)
            self.assertNotIn(case.expectation.rationale, serialized)
            self.assertNotIn("expectation", serialized)
            # The context owns a copy, so consumers cannot mutate the stored fixture.
            context.files["head"]["service.py"] = "changed\n"
            self.assertEqual(case.review_inputs()[1].read("service.py", "head"), case.fixture.head["service.py"])

    def test_tool_reads_unchanged_dependency_with_versioned_evidence(self):
        from review_tools import ContextBudget, ToolSession
        _, context = self.bug.review_inputs()
        session = ToolSession(context, ContextBudget())
        result = session.execute("read_file", json.dumps({
            "path": "store.py", "start_line": 1, "end_line": 4, "version": "head",
        }))
        self.assertIn("return CACHE", json.dumps(result))
        self.assertIn(context.revisions["head"], json.dumps(result))

    def test_fingerprints_ignore_json_format_but_track_gold_and_source(self):
        data = self.bug.model_dump()
        reordered = dict(reversed(list(data.items())))
        self.assertEqual(EvalCase.model_validate(reordered).case_hash, self.bug.case_hash)
        data["expectation"]["rationale"] += " Annotation correction."
        changed = EvalCase.model_validate(data)
        self.assertNotEqual(changed.case_hash, self.bug.case_hash)
        self.assertEqual(changed.review_inputs()[1].revisions, self.bug.review_inputs()[1].revisions)
        data["fixture"]["head"]["service.py"] += "# Additional source line\n"
        source_changed = EvalCase.model_validate(data)
        self.assertNotEqual(source_changed.review_inputs()[1].revisions["head"],
                            self.bug.review_inputs()[1].revisions["head"])
        self.assertEqual(source_changed.review_inputs()[1].revisions["base"],
                         self.bug.review_inputs()[1].revisions["base"])

    def test_rejects_invalid_fixture_and_annotations(self):
        changes = [
            (("schema_version",), 2),
            (("fixture_version",), "1"),
            (("split",), "test"),
            (("label",), "clean"),
            (("changed_files",), []),
            (("changed_files",), ["service.py", "service.py"]),
            (("changed_files",), ["store.py"]),
            (("expectation", "rationale"), "  "),
            (("expectation", "defects"), []),
            (("expectation", "evidence"), []),
            (("expectation", "evidence", 0, "version"), "working"),
            (("expectation", "evidence", 0, "start_line"), 0),
            (("expectation", "evidence", 0, "end_line"), 99),
            (("expectation", "evidence", 0, "end_line"), 1),
            (("expectation", "evidence", 0, "path"), "missing.py"),
            (("expectation", "evidence", 0, "quote"), "incorrect\n"),
            (("expectation", "defects", 0, "primary_location", "path"), "store.py"),
            (("fixture", "head", "service.py"), "no final newline"),
            (("fixture", "head", "service.py"), "CRLF\r\n"),
            (("fixture", "head", "service.py"), "binary\x00\n"),
            (("fixture", "head", "../outside.py"), "value = 1\n"),
            (("fixture", "head", ".env"), "value = 1\n"),
            (("unrecognized",), True),
        ]
        for keys, value in changes:
            data = copy.deepcopy(self.bug.model_dump())
            parent = data
            for key in keys[:-1]:
                parent = parent[key]
            parent[keys[-1]] = value
            with self.subTest(keys=keys, value=value), self.assertRaises(ValueError):
                EvalCase.model_validate(data)
        data = self.bug.model_dump()
        data["expectation"]["defects"] *= 2
        with self.assertRaisesRegex(ValueError, "Duplicate defect_id"):
            EvalCase.model_validate(data)
        data = self.clean.model_dump()
        del data["expectation"]["defects"]
        with self.assertRaises(ValueError):
            EvalCase.model_validate(data)

    def test_added_deleted_and_empty_files_are_preserved_in_inputs(self):
        data = self.clean.model_dump()
        data["fixture"]["base"].update({"removed.py": "old = 1\n", "empty_removed.py": ""})
        data["fixture"]["head"].update({"added.py": "new = 2\n", "empty_added.py": ""})
        data["changed_files"] += ["removed.py", "empty_removed.py", "added.py", "empty_added.py"]
        diffs, context = EvalCase.model_validate(data).review_inputs()
        by_path = {item.path: item.diff for item in diffs}
        self.assertIn("--- /dev/null", by_path["added.py"])
        self.assertIn("+++ /dev/null", by_path["removed.py"])
        self.assertIn("new file mode", by_path["empty_added.py"])
        self.assertIn("deleted file mode", by_path["empty_removed.py"])
        self.assertNotIn("removed.py", context.paths("head"))

    def test_dataset_rejects_empty_duplicate_and_split_leakage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "No evaluation cases"):
                load_cases(root)
            data = self.bug.model_dump()
            (root / "one.json").write_text(json.dumps(data), encoding="utf-8")
            (root / "two.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate case_id"):
                load_cases(root)
            data.update(case_id="another-shared-list", split="holdout")
            (root / "two.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "leaks across splits"):
                load_cases(root)

    def test_clean_gold_is_independent_of_execution_status(self):
        diffs, _ = self.clean.review_inputs()
        empty = ReviewResult(summary="", issues=[])
        with patch("review_pipeline.review_diff", return_value=empty) as model:
            complete = review_files(diffs)
            model.assert_called_once()
        with patch("review_pipeline.review_diff", side_effect=RuntimeError("model failed")):
            failed = review_files(diffs)
        with patch("review_pipeline.review_diff") as model:
            skipped = review_files(diffs, max_files=0)
            model.assert_not_called()
        self.assertEqual((complete.status, complete.reviewed_files, complete.eligible_files), ("COMPLETE", 1, 1))
        for batch in (failed, skipped):
            self.assertEqual((batch.status, batch.reviewed_files, batch.eligible_files), ("INCOMPLETE", 0, 1))
            self.assertEqual(batch.result.issues, [])
        self.assertEqual(self.clean.expectation.defects, [])

    def run_fixture(self, case, version, probe):
        # Only these checked-in synthetic examples are executed, never during load.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, source in getattr(case.fixture, version).items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source, encoding="utf-8", newline="\n")
            result = subprocess.run(
                [sys.executable, "-I", "-c", "import sys, json; sys.path.insert(0, sys.argv[1]); " + probe, directory],
                capture_output=True, text=True, timeout=10, check=True,
            )
        return json.loads(result.stdout)

    def test_bug_reproduces_only_in_head_and_clean_change_preserves_behavior(self):
        probe = "import service, store; result = service.ranked_values(); print(json.dumps([result, store.values()]))"
        self.assertEqual(self.run_fixture(self.bug, "base", probe), [[1, 2, 3], [3, 1, 2]])
        self.assertEqual(self.run_fixture(self.bug, "head", probe), [[1, 2, 3], [1, 2, 3]])
        for version in ("base", "head"):
            self.assertEqual(self.run_fixture(self.clean, version,
                "import service; print(json.dumps(service.display_name()))"), "Ada")

    def test_all_twenty_cases_match_executable_base_and_head_oracles(self):
        probes = json.loads((CASES_DIR.parent / "behavior.json").read_text(encoding="utf-8"))
        self.assertEqual(set(probes), set(self.cases))
        for case_id, case in self.cases.items():
            probe = probes[case_id]
            for version in ("base", "head"):
                with self.subTest(case=case_id, version=version):
                    # A separate process isolates module globals and one-shot state.
                    script = "exec(" + repr(probe["probe"]) + "); print(json.dumps(answer))"
                    self.assertEqual(self.run_fixture(case, version, script), probe[version])
            with self.subTest(case=case_id):
                if case.label == "clean":
                    self.assertEqual(probe["base"], probe["head"])
                else:
                    self.assertNotEqual(probe["base"], probe["head"])

    def test_holdout_families_are_paired_and_separate_from_development(self):
        dev = {c.scenario_id for c in self.cases.values() if c.split == "dev"}
        holdout = {c.scenario_id for c in self.cases.values() if c.split == "holdout"}
        self.assertFalse(dev & holdout)
        self.assertEqual(len(holdout), 4)
        for family in holdout:
            rows = [c for c in self.cases.values() if c.scenario_id == family]
            self.assertEqual(len(rows), 2)
            self.assertEqual({c.label for c in rows}, {"clean", "defect"})
        dev_snapshots = {fingerprint(c.fixture.model_dump()) for c in self.cases.values() if c.split == "dev"}
        self.assertTrue(all(fingerprint(c.fixture.model_dump()) not in dev_snapshots
                            for c in self.cases.values() if c.split == "holdout"))

    def test_frozen_manifest_rejects_drift_and_missing_cases(self):
        cases = list(self.cases.values())
        validate_dataset_lock(cases)
        with self.assertRaisesRegex(ValueError, "frozen manifest"):
            validate_dataset_lock(cases[:-1])
        for field, value in (("split", "holdout"), ("scenario_id", "renamed-family")):
            changed = self.bug.model_copy(update={field: value})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "frozen manifest"):
                validate_dataset_lock([changed if c.case_id == self.bug.case_id else c for c in cases])
        data = self.bug.model_dump()
        data["expectation"]["rationale"] += " changed"
        changed = EvalCase.model_validate(data)
        with self.assertRaisesRegex(ValueError, "frozen manifest"):
            validate_dataset_lock([changed if c.case_id == self.bug.case_id else c for c in cases])

    def test_cli_is_offline_and_does_not_import_model_clients(self):
        root = CASES_DIR.parents[1]
        script = """
import runpy, socket, sys
from unittest.mock import patch
sys.argv = ['evals.dataset']
with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')):
    try:
        runpy.run_module('evals.dataset', run_name='__main__')
    except SystemExit as exc:
        assert exc.code == 0
assert 'reviewer' not in sys.modules
assert 'openai' not in sys.modules
assert 'dotenv' not in sys.modules
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=root,
                                capture_output=True, text=True, timeout=15, check=True)
        report = json.loads(result.stdout)
        self.assertEqual(report["validation"], "passed")
        self.assertEqual(report["case_count"], 20)
        self.assertEqual(report["splits"]["holdout"], {"defect": 4, "clean": 4})
        self.assertTrue(report["lock_verified"])
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-m", "evals.dataset", "--cases-dir", directory],
                                    cwd=root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn("No evaluation cases", result.stderr)


if __name__ == "__main__":
    unittest.main()
