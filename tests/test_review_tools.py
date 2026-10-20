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
             ('search_code', '{"query":""}'), ('search_code', '{"query":"x","version":"other"}')]:
            with self.subTest(name=name, args=args), self.assertRaises(ContextError):
                ToolSession(context(), ContextBudget()).execute(name, args)

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
