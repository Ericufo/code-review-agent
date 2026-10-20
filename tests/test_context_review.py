import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from context_review import review_with_context
from diff_parser import FileDiff
from repository_context import ContextError
from review_pipeline import review_files
from review_tools import ContextBudget
from test_review_tools import context


def response(*calls, result=None, status='completed'):
    result = {'summary': '', 'issues': [], 'context_complete': True, 'context_reason': '', **(result or {})}
    return SimpleNamespace(status=status, output=[Mock(model_dump=Mock(return_value=call)) for call in calls],
                           output_text=json.dumps(result))


def call(name='read_file', arguments=None, call_id='call1'):
    return {'type': 'function_call', 'name': name, 'call_id': call_id,
            'arguments': json.dumps(arguments or {'path': 'dep.py', 'start_line': 1, 'end_line': 2})}


class TestContextReview(unittest.TestCase):
    def test_actual_graph_round_trip_and_evidence(self):
        seen = []
        def model(inputs, instructions, *, tools_enabled=True):
            if len(inputs) == 1:
                self.assertIn('untrusted data', instructions)
                return response(call())
            seen.extend(inputs)
            payload = json.loads(inputs[-1]['output'])
            evidence = payload['entries'][0]['evidence']
            return response(result={'summary': '', 'issues': [{'severity': 'high', 'file': 'invented.py',
                'title': 'None dereference', 'description': 'lookup returns None', 'evidence': [evidence]}]})
        batch = review_files([FileDiff('app.py', '+ lookup().name')], engine='langgraph',
                             context=context(), context_request_fn=model)
        self.assertFalse(batch.incomplete)
        issue = batch.result.issues[0]
        self.assertEqual(issue.file, 'app.py')
        self.assertEqual(issue.evidence[0].revision, 'a' * 40)
        self.assertEqual(seen[-1]['call_id'], 'call1')
        self.assertIn('return None', seen[-1]['output'])

    def test_context_can_exonerate_a_change(self):
        model = Mock(side_effect=[response(call()), response()])
        result = review_with_context('diff', context=context(), budget=ContextBudget(), request_fn=model)
        self.assertEqual(result.issues, [])
        self.assertEqual(model.call_count, 2)

    def test_tool_error_cannot_be_overridden_by_clean_final_answer(self):
        batch = review_files([FileDiff('bad.py', 'bad'), FileDiff('good.py', 'good')],
            engine='langgraph', context=context(), context_request_fn=Mock(side_effect=[
                response(call(arguments={'path': '../secret.py', 'start_line': 1, 'end_line': 1})),
                response(), response()]))
        self.assertTrue(batch.incomplete)
        self.assertEqual(batch.reviewed_files, 1)
        self.assertIn('context incomplete', batch.failures[0].reason)

    def test_fabricated_evidence_rejected(self):
        for evidence in ([], [{'repository': 'test/repo', 'revision': 'wrong', 'path': 'dep.py', 'start_line': 1, 'end_line': 2}]):
            result = {'summary': '', 'issues': [{'severity': 'high', 'file': 'app.py', 'title': 'x',
                                               'description': 'x', 'evidence': evidence}]}
            with self.subTest(evidence=evidence), self.assertRaisesRegex(ContextError, 'evidence'):
                review_with_context('diff', context=context(), budget=ContextBudget(),
                                    request_fn=Mock(return_value=response(result=result)))

    def test_rounds_and_global_calls_are_bounded(self):
        model = Mock(return_value=response(call()))
        with self.assertRaisesRegex(ContextError, 'final answer turn'):
            review_with_context('diff', context=context(), budget=ContextBudget(), request_fn=model, max_rounds=1)
        self.assertEqual(model.call_count, 1)
        model = Mock(side_effect=[response(call()), response()])
        batch = review_files([FileDiff('one.py', 'x'), FileDiff('two.py', 'y')], engine='langgraph',
            context=context(), context_request_fn=model, max_model_calls=2)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(batch.reviewed_files, 1)
        self.assertEqual(len(batch.skipped), 1)

    def test_timeout_invalid_response_and_duplicate_ids_fail(self):
        for reply in (TimeoutError('secret response body'), response(status='incomplete'),
                      response(call(), call())):
            model = Mock(side_effect=reply) if isinstance(reply, Exception) else Mock(return_value=reply)
            with self.subTest(reply=reply), self.assertRaises(ContextError) as caught:
                review_with_context('diff', context=context(), budget=ContextBudget(), request_fn=model)
            self.assertNotIn('secret response body', str(caught.exception))

    def test_trace_counts_tools_without_source_text(self):
        events = []
        batch = review_files([FileDiff('app.py', 'secret diff')], engine='langgraph', context=context(),
            context_request_fn=Mock(side_effect=[response(call()), response()]), on_step=events.append)
        self.assertFalse(batch.incomplete)
        self.assertIn('tool_result', [event.node for event in events])
        self.assertEqual(events[-1].model_calls, 2)
        self.assertEqual(events[-1].tool_calls, 1)
        self.assertNotIn('secret diff', repr(events))

    def test_excluded_files_never_reach_model(self):
        model = Mock()
        batch = review_files([FileDiff('.env', 'secret')], engine='langgraph', context=context(), context_request_fn=model)
        self.assertFalse(batch.incomplete)
        self.assertEqual(batch.status, 'NO_ELIGIBLE_FILES')
        self.assertEqual(len(batch.excluded), 1)
        model.assert_not_called()

    def test_request_disables_sdk_retries_and_bounds_timeout(self):
        from context_review import request_context_model
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'test'}), patch('context_review.OpenAI') as client:
            request_context_model([], 'instructions')
        self.assertEqual(client.call_args.kwargs['max_retries'], 0)
        self.assertEqual(client.call_args.kwargs['timeout'], 45)
        payload = client.return_value.__enter__.return_value.responses.create.call_args.kwargs
        self.assertEqual(payload['text'], {'format': {'type': 'json_object'}})
        self.assertEqual(payload['reasoning'], {'effort': 'low'})
        self.assertEqual(payload['max_output_tokens'], 8192)

    def test_final_request_actually_removes_tools_from_api_payload(self):
        from context_review import request_context_model
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'test'}), patch('context_review.OpenAI') as client:
            request_context_model([], 'final', tools_enabled=False)
        self.assertEqual(client.return_value.__enter__.return_value.responses.create.call_args.kwargs['tools'], [])

    def test_eight_tool_calls_finish_without_a_ninth_tool_request(self):
        events = []
        calls = [call('search_code', {'query': query}, call_id=str(i))
                 for i, query in enumerate(['lookup', 'return', 'name', 'missing', 'x', 'def', 'None', 'from'])]
        model = Mock(side_effect=[response(*calls), response()])
        result = review_with_context('diff', context=context(), budget=ContextBudget(),
                                     request_fn=model, on_event=events.append)
        self.assertEqual(result.issues, [])
        self.assertTrue(model.call_args_list[0].kwargs['tools_enabled'])
        self.assertFalse(model.call_args_list[1].kwargs['tools_enabled'])
        self.assertIn('finalize_request', events)

    def test_last_round_is_reserved_even_when_tools_remain(self):
        model = Mock(side_effect=[response(call()), response()])
        review_with_context('diff', context=context(), budget=ContextBudget(), request_fn=model, max_rounds=2)
        self.assertFalse(model.call_args_list[-1].kwargs['tools_enabled'])
        self.assertIn('FINAL ANSWER TURN', model.call_args.args[1])

    def test_final_answer_cannot_hide_insufficient_context(self):
        model = Mock(side_effect=[response(call()), response(result={
            'context_complete': False, 'context_reason': 'Need caller definition'})])
        batch = review_files([FileDiff('app.py', 'diff')], engine='langgraph',
                             context=context(), context_request_fn=model, max_model_calls=2)
        self.assertTrue(batch.incomplete)
        self.assertEqual(batch.reviewed_files, 0)
        self.assertIn('Need caller definition', batch.failures[0].reason)

    def test_context_status_is_required_even_when_issues_are_empty(self):
        reply = response()
        reply.output_text = '{"summary":"","issues":[]}'
        with self.assertRaisesRegex(ContextError, 'invalid review JSON'):
            review_with_context('diff', context=context(), budget=ContextBudget(),
                                request_fn=Mock(return_value=reply))

    def test_json_diagnostics_do_not_echo_model_or_source_text(self):
        reply = response()
        reply.output_text = 'PRIVATE_SOURCE not valid JSON'
        with self.assertRaises(ContextError) as caught:
            review_with_context('diff', context=context(), budget=ContextBudget(),
                                request_fn=Mock(return_value=reply))
        self.assertIn('json_invalid', str(caught.exception))
        self.assertNotIn('PRIVATE_SOURCE', str(caught.exception))

    def test_model_budget_does_not_reuse_file_limit(self):
        model = Mock(side_effect=[response(call()), response()])
        batch = review_files([FileDiff('one.py', 'diff'), FileDiff('two.py', 'diff')],
            engine='langgraph', context=context(), context_request_fn=model,
            max_files=1, max_model_calls=4)
        self.assertEqual(batch.reviewed_files, 1)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(len(batch.skipped), 1)

    def test_direct_graph_builder_supplies_a_context_budget(self):
        from review_graph import build_review_graph
        graph = build_review_graph(review_fn=Mock(), context=context(),
            context_request_fn=Mock(return_value=response()), max_files=1,
            max_file_chars=100, max_total_chars=100)
        state = graph.invoke({'files': [FileDiff('app.py', 'diff')], 'index': 0,
            'attempted': 0, 'used_chars': 0, 'reviewed': 0,
            'issues': [], 'failures': [], 'skipped': []})
        self.assertEqual(state['batch'].reviewed_files, 1)

    def test_duplicate_ids_across_rounds_and_repeated_arguments_are_rejected(self):
        for second in (call(), call(call_id='call2')):
            model = Mock(side_effect=[response(call()), response(second), response()])
            with self.subTest(second=second), self.assertRaises(ContextError):
                review_with_context('diff', context=context(), budget=ContextBudget(), request_fn=model)

    def test_diff_only_prompt_and_results_do_not_claim_context_evidence(self):
        from reviewer import get_review_schema
        from review_models import ReviewIssue, ReviewResult
        self.assertNotIn('ContextEvidence', get_review_schema())
        self.assertNotIn('evidence', get_review_schema())
        issue = ReviewIssue(severity='high', file='app.py', title='x', description='x', evidence=[{
            'repository': 'fake', 'revision': 'fake', 'path': 'dep.py', 'start_line': 1, 'end_line': 1}])
        with patch('review_pipeline.review_diff', return_value=ReviewResult(summary='', issues=[issue])):
            batch = review_files([FileDiff('app.py', 'diff')])
        self.assertEqual(batch.result.issues[0].evidence, [])
