import json
import unittest

from repository_context import ContextError, RepositoryContext
from review_tools import ContextBudget, ToolSession
from review_models import ContextEvidence, ReviewIssue, ReviewResult


def context():
    return RepositoryContext('test/repo', {'head': 'a' * 40, 'base': 'b' * 40},
        {'head': {'app.py': 'from dep import lookup\nx = lookup().name\n',
                  'dep.py': 'def lookup():\n    return None\n'},
         'base': {'dep.py': 'def lookup():\n    return object()\n'}})


class TestReviewTools(unittest.TestCase):
    def test_evidence_may_narrow_but_not_extend_or_change_version(self):
        session = ToolSession(context(), ContextBudget())
        session.execute('read_file', '{"path":"dep.py","start_line":1,"end_line":2}')
        known = session.evidence[0].model_dump()
        def result(evidence):
            return ReviewResult(summary='', issues=[ReviewIssue(severity='high', file='app.py',
                title='x', description='x', evidence=[ContextEvidence(**evidence)])])
        session.validate_evidence(result({**known, 'start_line': 2}))
        for overrides in ({'end_line': 3}, {'revision': 'invented'}, {'path': 'app.py'}, {'repository': 'other'}):
            with self.subTest(overrides=overrides), self.assertRaises(ContextError):
                session.validate_evidence(result({**known, **overrides}))

    def test_read_and_search_return_numbered_versioned_evidence(self):
        session = ToolSession(context(), ContextBudget())
        result = json.loads(session.execute('read_file', json.dumps(
            {'path': 'dep.py', 'start_line': 1, 'end_line': 2, 'version': 'base'})))
        self.assertIn('2:     return object()', result['entries'][0]['text'])
        self.assertEqual(result['entries'][0]['evidence']['revision'], 'b' * 40)
        result = json.loads(session.execute('search_code', '{"query":"return None"}'))
        self.assertEqual(result['entries'][0]['evidence']['path'], 'dep.py')
        self.assertEqual(result['entries'][0]['evidence']['start_line'], 2)
        self.assertEqual(session.budget.tool_calls, 2)

    def test_invalid_arguments_never_read_source(self):
        for name, args in [('shell', '{}'), ('read_file', '{'),
             ('read_file', '{"path":"../dep.py","start_line":1,"end_line":2}'),
             ('read_file', '{"path":"dep.py","start_line":"1","end_line":2}'),
             ('read_file', '{"path":"dep.py","start_line":true,"end_line":2}'),
             ('read_file', '{"path":"dep.py","start_line":2,"end_line":1}'),
             ('read_file', '{"path":"dep.py","start_line":1,"end_line":2,"extra":1}'),
             ('search_code', '{"query":"lookup","context_lines":-1}'),
             ('search_code', '{"query":"lookup","context_lines":true}'),
             ('search_code', '{"query":""}'), ('search_code', '{"query":"x","version":"other"}')]:
            with self.subTest(name=name, args=args), self.assertRaises(ContextError):
                ToolSession(context(), ContextBudget()).execute(name, args)

    def test_search_neighbors_supply_citable_signature_and_body_at_pinned_version(self):
        session = ToolSession(context(), ContextBudget())
        payload = session.execute('search_code', json.dumps({
            'query': 'def lookup', 'path': 'dep.py', 'version': 'base', 'context_lines': 5}))
        match = json.loads(payload)['entries'][0]
        self.assertEqual(match['text'], '1: def lookup():\n2:     return object()')
        self.assertEqual(match['match_line'], 1)
        self.assertFalse(match['context_truncated'])
        self.assertEqual(match['evidence'], {
            'repository': 'test/repo', 'revision': 'b' * 40, 'path': 'dep.py',
            'start_line': 1, 'end_line': 2})
        result = ReviewResult(summary='', issues=[ReviewIssue(
            severity='high', file='app.py', title='x', description='x',
            evidence=[ContextEvidence(**{**match['evidence'], 'start_line': 2})])])
        session.validate_evidence(result)
        self.assertEqual(session.used_chars, len(payload))

    def test_search_neighbors_shrink_to_budget_without_citing_omitted_lines(self):
        ctx = context()
        ctx.files['head']['long.py'] = 'x' * 3000 + '\nneedle\n' + 'y' * 3000
        session = ToolSession(ctx, ContextBudget(max_context_chars=1000))
        payload = session.execute('search_code', json.dumps({
            'query': 'needle', 'path': 'long.py', 'context_lines': 1}))
        match = json.loads(payload)['entries'][0]
        self.assertLessEqual(len(payload), 1000)
        self.assertTrue(match['context_truncated'])
        self.assertEqual(match['text'], '2: needle')
        self.assertEqual((match['evidence']['start_line'], match['evidence']['end_line']), (2, 2))
        self.assertEqual(session.evidence[0].start_line, 2)
        self.assertEqual(session.evidence[0].end_line, 2)

    def test_oversized_search_context_is_clamped_and_evidence_stays_bounded(self):
        ctx = context()
        ctx.files['head']['long.py'] = '\n'.join(
            'needle' if i == 50 else f'line {i}' for i in range(1, 101))
        for requested in (21, 1000000):
            with self.subTest(requested=requested):
                session = ToolSession(ctx, ContextBudget())
                payload = session.execute('search_code', json.dumps({
                    'query': 'needle', 'path': 'long.py', 'context_lines': requested}))
                page = json.loads(payload)
                match = page['entries'][0]
                self.assertTrue(match['context_truncated'])
                self.assertEqual(match['match_line'], 50)
                self.assertEqual((match['evidence']['start_line'], match['evidence']['end_line']), (30, 70))
                self.assertEqual(len(match['text'].splitlines()), 41)
                self.assertLessEqual(len(payload), 12000)
                self.assertFalse(page['has_more'])
                self.assertEqual(session.used_chars, len(payload))

    def test_search_neighbor_pagination_counts_matches_even_with_overlapping_ranges(self):
        ctx = context()
        ctx.files['head']['many.py'] = 'needle\n' * 25
        session = ToolSession(ctx, ContextBudget())
        entries, offset = [], 0
        while True:
            page = json.loads(session.execute('search_code', json.dumps({
                'query': 'needle', 'path': 'many.py', 'context_lines': 2, 'offset': offset})))
            entries.extend(page['entries'])
            if not page['has_more']:
                break
            offset = page['next_offset']
            self.assertEqual(offset, len(entries))
        self.assertEqual([item['match_line'] for item in entries], list(range(1, 26)))
        for item in entries:
            evidence = item['evidence']
            self.assertLessEqual(evidence['start_line'], item['match_line'])
            self.assertGreaterEqual(evidence['end_line'], item['match_line'])

    def test_repeated_calls_charge_budget_and_stop(self):
        session = ToolSession(context(), ContextBudget())
        session.execute('search_code', '{"query":"lookup"}')
        with self.assertRaisesRegex(ContextError, 'Repeated'):
            session.execute('search_code', '{"version":"head","query":"lookup"}')
        self.assertEqual(session.budget.tool_calls, 2)

    def test_no_silent_result_truncation(self):
        ctx = context()
        ctx.files['head']['huge.py'] = 'x' * 12001
        session = ToolSession(ctx, ContextBudget())
        with self.assertRaisesRegex(ContextError, 'single source line'):
            session.execute('read_file', '{"path":"huge.py","start_line":1,"end_line":1}')
        self.assertEqual(session.evidence, [])
        ctx.files['head']['many.py'] = 'match\n' * 21
        first = json.loads(session.execute('search_code', '{"query":"match"}'))
        self.assertEqual(len(first['entries']), 20)
        self.assertTrue(first['has_more'])
        second = json.loads(session.execute('search_code', '{"query":"match","offset":20}'))
        self.assertEqual(len(second['entries']), 1)
        self.assertFalse(second['has_more'])
        self.assertEqual(second['entries'][0]['evidence']['start_line'], 21)

    def test_missing_and_out_of_range_sources_fail(self):
        session = ToolSession(context(), ContextBudget())
        for args in ('{"path":"missing.py","start_line":1,"end_line":1}',
                     '{"path":"dep.py","start_line":10,"end_line":10}'):
            with self.assertRaises(ContextError):
                session.execute('read_file', args)

    def test_read_clamps_end_at_eof_and_reports_actual_evidence_range(self):
        session = ToolSession(context(), ContextBudget())
        data = json.loads(session.execute('read_file', '{"path":"dep.py","start_line":1,"end_line":120}'))
        self.assertEqual(data['total_lines'], 2)
        self.assertEqual(data['entries'][0]['evidence']['end_line'], 2)

    def test_empty_source_read_is_explicit_and_has_no_evidence(self):
        ctx = context()
        ctx.files['head']['empty.py'] = ''
        data = json.loads(ToolSession(ctx, ContextBudget()).execute('read_file',
            '{"path":"empty.py","start_line":1,"end_line":10}'))
        self.assertEqual(data['total_lines'], 0)
        self.assertEqual(data['entries'], [])

    def test_tool_context_model_input_and_time_budgets(self):
        for budget in (ContextBudget(max_tool_calls=0), ContextBudget(max_context_chars=1)):
            with self.assertRaises(ContextError):
                ToolSession(context(), budget).execute('search_code', '{"query":"lookup"}')
        for budget in (ContextBudget(max_model_calls=0), ContextBudget(max_input_chars=1)):
            with self.assertRaises(ContextError):
                budget.model_request([{'content': 'input'}])
        ticks = iter([0, 181])
        session = ToolSession(context(), ContextBudget(), clock=lambda: next(ticks))
        with self.assertRaisesRegex(ContextError, 'time budget'):
            session.execute('search_code', '{"query":"lookup"}')
