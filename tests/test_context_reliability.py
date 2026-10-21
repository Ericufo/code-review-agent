import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from context_review import review_with_context
from diff_parser import FileDiff
from repository_context import ContextError
from review_pipeline import review_files
from review_tools import ContextBudget, ModelBudgetError, ToolSession, MAX_FILE_TOOL_CALLS
from test_context_review import response, call
from test_review_tools import context


class TestContextReliability(unittest.TestCase):
    def test_read_page_shrinks_to_remaining_file_or_batch_budget(self):
        for limiting in ('file', 'batch'):
            with self.subTest(limiting=limiting):
                ctx = context()
                ctx.files['head']['long.py'] = '\n'.join('x' * 100 for _ in range(80))
                budget = ContextBudget(max_context_chars=1600 if limiting == 'batch' else 256000)
                session = ToolSession(ctx, budget, max_chars=1600 if limiting == 'file' else 48000)
                session.used_chars = budget.context_chars = 200
                payload = session.execute('read_file', '{"path":"long.py","start_line":1,"end_line":80}')
                page = json.loads(payload)
                self.assertLessEqual(len(payload), 1400)
                self.assertEqual(session.used_chars, 200 + len(payload))
                self.assertEqual(budget.context_chars, 200 + len(payload))
                self.assertTrue(page['has_more'])
                evidence = page['entries'][0]['evidence']
                self.assertEqual(page['next_start_line'], evidence['end_line'] + 1)
                self.assertLess(evidence['end_line'], 80)
                self.assertTrue(page['entries'][0]['text'].endswith('x' * 100))

    def test_search_page_shrinks_without_losing_continuation(self):
        ctx = context()
        ctx.files['head']['long.py'] = 'needle\n' * 20
        budget = ContextBudget()
        session = ToolSession(ctx, budget, max_chars=1500)
        first = json.loads(session.execute('search_code', '{"query":"needle","path":"long.py"}'))
        self.assertTrue(first['has_more'])
        self.assertEqual(first['next_offset'], len(first['entries']))
        # A fresh session with sufficient budget can resume the exact same search.
        second = json.loads(ToolSession(ctx, ContextBudget()).execute('search_code', json.dumps(
            {'query': 'needle', 'path': 'long.py', 'offset': first['next_offset']})))
        self.assertEqual([e['evidence']['start_line'] for e in first['entries'] + second['entries']],
                         list(range(1, 21)))

    def test_too_little_context_fails_without_charging_or_citable_evidence(self):
        session = ToolSession(context(), ContextBudget(), max_chars=10)
        with self.assertRaisesRegex(ContextError, 'Remaining context character budget'):
            session.execute('read_file', '{"path":"dep.py","start_line":1,"end_line":2}')
        self.assertEqual(session.used_chars, 0)
        self.assertEqual(session.budget.context_chars, 0)
        self.assertEqual(session.evidence, [])
        self.assertEqual(session.calls, 1)

    def test_large_requested_range_pages_by_lines_and_stops_at_eof(self):
        ctx = context()
        ctx.files['head']['long.py'] = '\n'.join(str(i) for i in range(250))
        session = ToolSession(ctx, ContextBudget())
        ranges, start = [], 1
        while True:
            page = json.loads(session.execute('read_file', json.dumps(
                {'path': 'long.py', 'start_line': start, 'end_line': 10000})))
            evidence = page['entries'][0]['evidence']
            ranges.append((evidence['start_line'], evidence['end_line']))
            if not page['has_more']:
                break
            start = page['next_start_line']
        self.assertEqual(ranges, [(1, 120), (121, 240), (241, 250)])
        self.assertIsNone(page['next_start_line'])

    def test_argument_diagnostics_explain_constraints_without_echoing_values(self):
        session = ToolSession(context(), ContextBudget())
        with self.assertRaisesRegex(ContextError, 'start_line: int_type') as caught:
            session.execute('read_file', '{"path":"dep.py","start_line":"secret-value","end_line":2}')
        self.assertNotIn('secret-value', str(caught.exception))
        with self.assertRaisesRegex(ContextError, 'Line range must be ordered'):
            session.execute('read_file', '{"path":"dep.py","start_line":2,"end_line":1}')

    def test_long_read_pages_are_bounded_contiguous_and_citable(self):
        ctx = context()
        ctx.files['head']['long.py'] = '\n'.join(f'{i} ' + 'x' * 200 for i in range(100))
        session = ToolSession(ctx, ContextBudget())
        start, received = 1, []
        while True:
            payload = session.execute('read_file', json.dumps({'path': 'long.py', 'start_line': start, 'end_line': 100}))
            self.assertLessEqual(len(payload), 12_000)
            page = json.loads(payload)
            evidence = page['entries'][0]['evidence']
            received.extend(range(evidence['start_line'], evidence['end_line'] + 1))
            if not page['has_more']:
                break
            self.assertEqual(page['next_start_line'], evidence['end_line'] + 1)
            start = page['next_start_line']
        self.assertEqual(received, list(range(1, 101)))

    def test_search_scope_and_pagination_do_not_skip_matches(self):
        ctx = context()
        ctx.files['head']['a.py'] = 'needle\n' * 25
        ctx.files['head']['b.py'] = 'needle\n' * 10
        session = ToolSession(ctx, ContextBudget())
        first = json.loads(session.execute('search_code', '{"query":"needle","path":"a.py"}'))
        second = json.loads(session.execute('search_code', '{"query":"needle","path":"a.py","offset":20}'))
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        entries = first['entries'] + second['entries']
        self.assertEqual([e['evidence']['start_line'] for e in entries], list(range(1, 26)))
        self.assertTrue(all(e['evidence']['path'] == 'a.py' for e in entries))
        with self.assertRaises(ContextError):
            session.execute('search_code', '{"query":"needle","path":"../a.py"}')

    def test_input_budget_error_is_specific_and_does_not_charge_rejected_request(self):
        budget = ContextBudget(max_input_chars=1)
        with self.assertRaisesRegex(ModelBudgetError, 'input character.*used=0.*next_request=.*limit=1'):
            budget.model_request([{'role': 'user', 'content': 'hello'}])
        self.assertEqual((budget.model_calls, budget.input_chars), (0, 0))
        with self.assertRaisesRegex(ModelBudgetError, 'model call budget'):
            ContextBudget(max_model_calls=0).model_request([])

    def test_file_rejected_before_dispatch_is_skipped_and_trace_exposes_limits(self):
        model, events = Mock(), []
        batch = review_files([FileDiff('app.py', 'diff')], engine='langgraph', context=context(),
                             context_request_fn=model, max_input_chars=1, on_step=events.append)
        model.assert_not_called()
        self.assertEqual(len(batch.skipped), 1)
        self.assertEqual(len(batch.failures), 0)
        self.assertIn('input character', batch.skipped[0].reason)
        self.assertEqual(events[-1].input_limit, 1)
        self.assertEqual(events[-1].input_chars, 0)
        self.assertEqual(events[-1].attempted, 0)

    def test_exhausted_model_budget_does_not_hide_ineligible_file_reasons(self):
        model = Mock()
        batch = review_files([
            FileDiff('missing.py', '', skip_reason='patch unavailable'),
            FileDiff('binary.py', 'Binary files a/x and b/x differ\n'),
            FileDiff('large.py', 'x' * 51), FileDiff('eligible.py', 'diff'),
        ], engine='langgraph', context=context(), context_request_fn=model,
            max_model_calls=0, max_file_chars=50)
        model.assert_not_called()
        self.assertEqual([item.reason for item in batch.skipped], [
            'patch unavailable', 'binary diff is not supported',
            'diff exceeds 50 characters', 'batch model call budget exhausted',
        ])

    def test_input_budget_exhausted_mid_file_is_failure(self):
        budget = ContextBudget()
        def model(inputs, instructions, **kwargs):
            budget.max_input_chars = budget.input_chars
            return response(call())
        with self.assertRaises(ModelBudgetError):
            review_with_context('diff', context=context(), budget=budget, request_fn=model)
        self.assertEqual(budget.model_calls, 1)

    def test_one_format_repair_is_charged_and_disables_tools(self):
        bad = response()
        bad.output_text = 'not JSON'
        model = Mock(side_effect=[bad, response()])
        budget, events = ContextBudget(), []
        result = review_with_context('diff', context=context(), budget=budget,
                                     request_fn=model, on_event=events.append)
        self.assertTrue(result.context_complete)
        self.assertEqual(budget.model_calls, 2)
        self.assertFalse(model.call_args.kwargs['tools_enabled'])
        self.assertIn('repair_request', events)
        model = Mock(return_value=bad)
        with self.assertRaisesRegex(ContextError, 'invalid review JSON'):
            review_with_context('diff', context=context(), budget=ContextBudget(), request_fn=model)
        self.assertEqual(model.call_count, 2)

    def test_format_repair_never_exceeds_global_call_budget(self):
        bad = response()
        bad.output_text = 'bad'
        model = Mock(return_value=bad)
        with self.assertRaises(ContextError):
            review_with_context('diff', context=context(), budget=ContextBudget(max_model_calls=1), request_fn=model)
        self.assertEqual(model.call_count, 1)

    def test_last_normal_round_can_repair_invalid_or_truncated_output_once(self):
        bad_json = response()
        bad_json.output_text = 'not JSON'
        truncated = response(status='incomplete')
        truncated.incomplete_details = SimpleNamespace(reason='max_output_tokens')
        for bad in (bad_json, truncated):
            with self.subTest(status=bad.status):
                model = Mock(side_effect=[response(call()), bad, response()])
                budget, events = ContextBudget(), []
                result = review_with_context('diff', context=context(), budget=budget,
                    request_fn=model, max_rounds=2, on_event=events.append)
                self.assertTrue(result.context_complete)
                self.assertEqual(budget.model_calls, 3)
                self.assertEqual(budget.tool_calls, 1)
                self.assertFalse(model.call_args_list[1].kwargs['tools_enabled'])
                self.assertFalse(model.call_args_list[2].kwargs['tools_enabled'])
                self.assertEqual(events.count('repair_request'), 1)
                self.assertIn('Remaining model requests including this one: 1', model.call_args.args[1])

    def test_final_round_repair_cannot_repeat_or_bypass_input_budget(self):
        bad = response()
        bad.output_text = 'not JSON'
        model = Mock(return_value=bad)
        with self.assertRaisesRegex(ContextError, 'invalid review JSON'):
            review_with_context('diff', context=context(), budget=ContextBudget(),
                                request_fn=model, max_rounds=1)
        self.assertEqual(model.call_count, 2)
        budget = ContextBudget()
        def exhaust_input(*args, **kwargs):
            budget.max_input_chars = budget.input_chars
            return bad
        model = Mock(side_effect=exhaust_input)
        with self.assertRaises(ModelBudgetError):
            review_with_context('diff', context=context(), budget=budget,
                                request_fn=model, max_rounds=1)
        self.assertEqual(model.call_count, 1)

    def test_truncated_output_can_retry_once_but_filtered_output_cannot(self):
        truncated = response(status='incomplete')
        truncated.incomplete_details = SimpleNamespace(reason='max_output_tokens')
        model = Mock(side_effect=[truncated, response()])
        review_with_context('diff', context=context(), budget=ContextBudget(), request_fn=model)
        self.assertEqual(model.call_count, 2)
        truncated.incomplete_details.reason = 'content_filter'
        model = Mock(return_value=truncated)
        with self.assertRaisesRegex(ContextError, 'content_filter'):
            review_with_context('diff', context=context(), budget=ContextBudget(), request_fn=model)
        self.assertEqual(model.call_count, 1)

    def test_final_message_is_parsed_without_commentary(self):
        final_json = response().output_text
        reply = response(
            {'type': 'message', 'phase': 'commentary', 'content': [{'type': 'output_text', 'text': 'I will review now.'}]},
            {'type': 'message', 'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': '```json\n' + final_json + '\n```'}]})
        reply.output_text = 'I will review now.\n' + final_json
        model = Mock(return_value=reply)
        result = review_with_context('diff', context=context(), budget=ContextBudget(), request_fn=model)
        self.assertTrue(result.context_complete)
        self.assertEqual(model.call_count, 1)

    def test_excess_tool_batch_returns_denials_without_exceeding_execution_budget(self):
        requests = [call('search_code', {'query': f'query{i}'}, str(i))
                    for i in range(MAX_FILE_TOOL_CALLS + 1)]
        model = Mock(side_effect=[response(*requests), response()])
        budget = ContextBudget()
        with self.assertRaisesRegex(ContextError, 'context incomplete'):
            review_with_context('diff', context=context(), budget=budget, request_fn=model)
        self.assertEqual(budget.tool_calls, MAX_FILE_TOOL_CALLS)
        self.assertEqual(len([item for item in model.call_args.args[0]
                              if item.get('type') == 'function_call_output']), MAX_FILE_TOOL_CALLS + 1)

    def test_diff_only_prompt_does_not_require_context_evidence(self):
        from reviewer import build_review_instructions
        prompt = build_review_instructions()
        self.assertNotIn('evidence', prompt)
        self.assertNotIn('context_complete', prompt)
        self.assertIn('context_complete', build_review_instructions(include_evidence=True, require_context_status=True))

