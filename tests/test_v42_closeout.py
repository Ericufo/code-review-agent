import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

import main
from context_review import review_with_context
from diff_parser import FileDiff
from github_diff import github_files_to_diff
from repository_context import ContextError
from review_formatter import format_review_batch
from review_models import ReviewResult
from review_pipeline import review_files
from review_tools import ContextBudget, ToolSession
from test_context_review import response, call
from test_review_tools import context


class TestCloseoutScope(unittest.TestCase):
    def test_exclusions_precede_patch_size_and_budget_without_spending_calls(self):
        events, model = [], Mock(return_value=response())
        batch = review_files([
            FileDiff('README.md', '', skip_reason='patch unavailable'),
            FileDiff('records.jsonl', 'x' * 20001), FileDiff('app.py', 'diff'),
        ], engine='langgraph', context=context(), context_request_fn=model,
            max_model_calls=1, on_step=events.append)
        self.assertEqual((batch.total_files, batch.eligible_files, batch.reviewed_files), (3, 1, 1))
        self.assertEqual(len(batch.excluded), 2)
        self.assertEqual(batch.status, 'COMPLETE')
        self.assertEqual((events[-1].attempted, events[-1].used_chars, events[-1].excluded), (1, 4, 2))
        self.assertEqual(model.call_count, 1)
        self.assertIn('Reviewed 1/1', batch.coverage_summary)
        rendered = format_review_batch(batch)
        self.assertIn('Excluded files (outside review scope)', rendered)
        self.assertNotIn('Review is incomplete', rendered)

    def test_all_excluded_is_not_a_claim_of_successful_code_review(self):
        model = Mock()
        batch = review_files([FileDiff('README.md', 'text')], engine='langgraph',
                             context=context(), context_request_fn=model)
        model.assert_not_called()
        self.assertEqual(batch.status, 'NO_ELIGIBLE_FILES')
        self.assertFalse(batch.incomplete)
        self.assertIn('No eligible files', batch.result.summary)
        self.assertNotIn('No issues found', format_review_batch(batch))

    def test_in_scope_failure_and_budget_skip_still_mean_incomplete(self):
        model = Mock(return_value=response(result={'context_complete': False, 'context_reason': 'Missing definition'}))
        batch = review_files([FileDiff('README.md', 'text'), FileDiff('app.py', 'diff'),
                              FileDiff('dep.py', 'diff')], engine='langgraph', context=context(),
                             context_request_fn=model, max_model_calls=1)
        self.assertEqual((len(batch.excluded), len(batch.failures), len(batch.skipped)), (1, 1, 1))
        self.assertEqual(batch.status, 'INCOMPLETE')
        self.assertIn('Reviewed 0/2', batch.coverage_summary)

    def test_local_and_pr_adapters_share_scope_but_diff_only_keeps_markdown(self):
        for files in ([FileDiff('README.md', 'text')], github_files_to_diff([
                {'filename': 'README.md', 'patch': '+text'}])):
            model = Mock()
            batch = review_files(files, engine='langgraph', context=context(), context_request_fn=model)
            self.assertEqual(batch.status, 'NO_ELIGIBLE_FILES')
            model.assert_not_called()
            with patch('review_pipeline.review_diff', return_value=ReviewResult(summary='', issues=[])) as plain:
                batch = review_files(files, engine='langgraph')
            self.assertEqual(batch.reviewed_files, 1)
            plain.assert_called_once()


class TestCloseoutCLI(unittest.TestCase):
    def run_cli(self, *args):
        output = io.StringIO()
        with patch('sys.argv', ['main.py', *args]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main.main()
        return code, output.getvalue()

    def test_selected_paths_only_review_selection_and_still_check_snapshot(self):
        ctx = context()
        ctx.ensure_unchanged = Mock()
        files = [FileDiff('app.py', 'one'), FileDiff('dep.py', 'two')]
        with patch('review_service.get_local_files', return_value=files), patch('review_service.capture_local_context', return_value=ctx), \
             patch('context_review.request_context_model', return_value=response()) as model:
            code, output = self.run_cli('--context', 'repo', '--paths', 'dep.py')
        self.assertEqual(code, 0)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(json.loads(model.call_args.args[0][0]['content'])['git_diff'], 'two')
        ctx.ensure_unchanged.assert_called()
        self.assertIn('Selection covers 1/2', output)
        self.assertIn('dep.py', output)

    def test_missing_selection_fails_before_api_and_subset_pr_is_rejected(self):
        with patch('review_service.get_local_files', return_value=[FileDiff('app.py', 'one')]), \
             patch('review_service.review_files') as review:
            code, output = self.run_cli('--paths', 'typo.py')
        self.assertEqual(code, 1)
        self.assertIn('must have current Git changes', output)
        review.assert_not_called()
        for args in (('--github-pr', '7', '--paths', 'app.py'), ('--no-publish',)):
            with self.assertRaises(SystemExit):
                self.run_cli(*args)

    def test_only_excluded_cli_is_exit_zero_without_model(self):
        ctx = context()
        ctx.ensure_unchanged = Mock()
        with patch('review_service.get_local_files', return_value=[FileDiff('README.md', 'text')]), \
             patch('review_service.capture_local_context', return_value=ctx), \
             patch('context_review.request_context_model') as model:
            code, output = self.run_cli('--context', 'repo')
        self.assertEqual(code, 0)
        self.assertIn('Status: NO_ELIGIBLE_FILES', output)
        self.assertIn('[EXCLUDED] README.md', output)
        self.assertIn('candidates: 0; excluded: 1', output)
        model.assert_not_called()


class TestCloseoutModel(unittest.TestCase):
    def test_context_schema_requires_nonempty_evidence_but_classic_does_not(self):
        from reviewer import get_review_schema
        schema = json.loads(get_review_schema(require_context_status=True))
        issue = schema['$defs']['ContextReviewIssue']
        self.assertIn('evidence', issue['required'])
        self.assertEqual(issue['properties']['evidence']['minItems'], 1)
        self.assertNotIn('evidence', get_review_schema())

    def test_missing_evidence_gets_one_repair_using_already_read_evidence(self):
        issue = {'severity': 'high', 'file': 'app.py', 'title': 'Null access', 'description': 'Lookup can return None'}
        def model(inputs, instructions, **kwargs):
            if mock.call_count == 1:
                return response(call())
            if mock.call_count == 2:
                return response(result={'issues': [issue]})
            evidence = json.loads(next(item['output'] for item in inputs
                                      if item.get('type') == 'function_call_output'))['entries'][0]['evidence']
            self.assertFalse(kwargs['tools_enabled'])
            return response(result={'issues': [{**issue, 'evidence': [evidence]}]})
        mock = Mock(side_effect=model)
        budget = ContextBudget()
        result = review_with_context('diff', context=context(), budget=budget, request_fn=mock)
        self.assertEqual((budget.model_calls, budget.tool_calls), (3, 1))
        self.assertEqual(result.issues[0].evidence[0].path, 'dep.py')

    def test_fabricated_evidence_is_not_treated_as_repairable_format_error(self):
        model = Mock(return_value=response(result={'issues': [{
            'severity': 'high', 'file': 'app.py', 'title': 'Claim', 'description': 'Unverified',
            'evidence': [{'repository': 'test/repo', 'revision': 'wrong', 'path': 'dep.py',
                          'start_line': 1, 'end_line': 2}],
        }]}))
        with self.assertRaisesRegex(ContextError, 'evidence'):
            review_with_context('diff', context=context(), budget=ContextBudget(), request_fn=model)
        self.assertEqual(model.call_count, 1)

    def test_model_receives_versioned_relative_inventory_and_explicit_final_request(self):
        model = Mock(return_value=response())
        review_with_context('diff', context=context(), budget=ContextBudget(max_model_calls=1), request_fn=model)
        inputs = model.call_args.args[0]
        inventory = json.loads(inputs[0]['content'])['available_sources']
        self.assertEqual(inventory['head'], ['app.py', 'dep.py'])
        self.assertEqual(inventory['base'], ['dep.py'])
        self.assertEqual(inputs[-1]['role'], 'user')
        self.assertIn('nonempty JSON', inputs[-1]['content'])
        self.assertFalse(model.call_args.kwargs['tools_enabled'])

    def test_tool_limit_error_distinguishes_file_and_batch(self):
        for session, label in ((ToolSession(context(), ContextBudget(), max_calls=0), 'file'),
                               (ToolSession(context(), ContextBudget(max_tool_calls=0)), 'batch')):
            with self.assertRaisesRegex(ContextError, rf'\({label}\): used=0, limit=0'):
                session.execute('read_file', '{"path":"dep.py","start_line":1,"end_line":2}')

    def test_rejected_tool_error_names_tool_without_echoing_bad_path(self):
        model = Mock(side_effect=[response(call(arguments={'path': '../PRIVATE.py', 'start_line': 1, 'end_line': 2})), response()])
        with self.assertRaisesRegex(ContextError, 'read_file: Invalid tool arguments') as caught:
            review_with_context('diff', context=context(), budget=ContextBudget(), request_fn=model)
        self.assertNotIn('PRIVATE', str(caught.exception))

