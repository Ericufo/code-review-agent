"""Fresh semantic checks for the annotation audit; no behavior.json or gold oracle.

Only trusted, checked-in synthetic sources are executed, in separate processes.
These checks corroborate an AI review, not independent human adjudication.
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from evals.dataset import load_cases


class TestAnnotationAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case.case_id: case for case in load_cases()}

    def observe(self, case_id, version, script):
        with tempfile.TemporaryDirectory() as directory:
            for name, source in getattr(self.cases[case_id].fixture, version).items():
                target = Path(directory, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source, encoding="utf-8", newline="\n")
            result = subprocess.run(
                [sys.executable, "-I", "-c",
                 "import sys, json; sys.path.insert(0, sys.argv[1]); "
                 + "exec(" + repr(script) + "); print(json.dumps(answer))", directory],
                check=True, capture_output=True, text=True, timeout=10,
            )
        return json.loads(result.stdout)

    def test_returned_alias_exposes_shared_state(self):
        script = """
import service, store
result = service.ranked_values()
same = result is store.CACHE
result.append(99)
answer = [same, store.CACHE]
"""
        self.assertEqual(self.observe("shared-list-sort", "base", script), [False, [3, 1, 2]])
        self.assertEqual(self.observe("shared-list-sort", "head", script), [True, [1, 2, 3, 99]])

    def test_mutating_returned_bucket_cannot_poison_copying_helper(self):
        script = """
import service
first = service.collect('a')
first.append('external')
second = service.collect('b')
explicit = ['seed']
third = service.collect('c', explicit)
answer = [first, second, first is second, explicit, third, service.collect('d', None)]
"""
        clean = [['a', 'external'], ['b'], False, ['seed'], ['seed', 'c'], ['d']]
        mutable_base = [['a', 'external'], ['b'], False, ['seed', 'c'], ['seed', 'c'], ['d']]
        mutable_head = [['a', 'external', 'b'], ['a', 'external', 'b'], True,
                        ['seed', 'c'], ['seed', 'c'], ['d']]
        for version in ("base", "head"):
            self.assertEqual(self.observe("copied-default-bucket", version, script), clean)
            self.assertEqual(self.observe("persistent-default-bucket", version, script),
                             mutable_base if version == "base" else mutable_head)

    def test_repeated_awaits_execute_each_write_or_return_unexecuted_coroutines(self):
        script = """
import asyncio, inspect, service, storage
async def probe():
    observed = []
    for _ in range(2):
        value = await service.save()
        if inspect.iscoroutine(value):
            value.close()
            observed.append('coroutine')
        else:
            observed.append(value)
    return [observed, storage.SAVED]
answer = asyncio.run(probe())
"""
        for case_id in ("unawaited-persist", "synchronous-persist"):
            for version in ("base", "head"):
                with self.subTest(case=case_id, version=version):
                    expected = ([['coroutine', 'coroutine'], []]
                                if (case_id, version) == ("unawaited-persist", "head")
                                else [[True, True], ['row', 'row']])
                    self.assertEqual(self.observe(case_id, version, script), expected)

    def test_path_components_and_rejected_child_names(self):
        names = ['A9', '0', 'a/b', '..', '../database/x', '../data2/x', '', '\u6c49', 'a.b', './x']
        script = f"import service\nanswer = [service.allowed(name) for name in {names!r}]"
        clean = [True, True, False, False, False, False, False, False, False, False]
        base = [True, True, True, False, False, False, True, True, True, True]
        head = [True, True, True, False, True, True, True, True, True, True]
        for version in ("base", "head"):
            self.assertEqual(self.observe("validated-child-name", version, script), clean)
            self.assertEqual(self.observe("sibling-path-prefix", version, script),
                             base if version == "base" else head)

    def test_expiry_policy_on_integer_grid(self):
        script = "import service\nanswer = [service.usable(n, e) for n in range(-2, 3) for e in range(-2, 3)]"
        exclusive = [n < e for n in range(-2, 3) for e in range(-2, 3)]
        inclusive = [n <= e for n in range(-2, 3) for e in range(-2, 3)]
        for version in ("base", "head"):
            self.assertEqual(self.observe("inclusive-expiry-inlined", version, script), inclusive)
            self.assertEqual(self.observe("exclusive-expiry-relaxed", version, script),
                             exclusive if version == "base" else inclusive)


if __name__ == "__main__":
    unittest.main()
