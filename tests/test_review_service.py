"""Offline service integration and persistence fault injection; no live clients."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from diff_parser import FileDiff
from github_review import PullRequestRevision, ReviewSupersededError
from review_checkpoint import PersistenceGuard, ReviewPersistenceError
from review_models import ReviewIssue, ReviewResult
from review_service import (
    PreparedReview, ReviewConfig, execute_review, prepare_github_review, prepare_local_review,
)
from test_context_review import call, response
from test_review_tools import context


class MemorySink:
    def __init__(self):
        self.events, self.files, self.batches = [], [], []

    def record_event(self, event):
        self.events.append(event)

    def file_completed(self, checkpoint):
        self.files.append(checkpoint)

    def finish(self, batch):
        self.batches.append(batch)


def prepared(*files, config=None, ctx=None):
    config = config or ReviewConfig(context_mode='repo')
    return PreparedReview(tuple(files or [FileDiff('app.py', '+lookup().name')]), config,
                          (ctx or context()) if config.context_mode == 'repo' else None)


class TestReviewService(unittest.TestCase):
    def test_import_has_no_io_environment_or_framework_side_effects(self):
        script = '''
import sys
from unittest.mock import patch
import dotenv, requests, openai, threading, subprocess, socket
def forbidden(*args, **kwargs):
    raise AssertionError("Import caused an external side effect")
with patch.object(dotenv, 'load_dotenv', forbidden), \\
     patch.object(requests.sessions.Session, 'request', forbidden), \\
     patch.object(openai, 'OpenAI', forbidden), \\
     patch.object(threading.Thread, 'start', forbidden), \\
     patch.object(subprocess, 'Popen', forbidden), \\
     patch.object(socket.socket, 'connect', forbidden):
    import review_service
    import main
assert not any(n.split('.')[0] in ('fastapi', 'sqlalchemy', 'psycopg', 'evals') for n in sys.modules)
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                                text=True, cwd=Path(__file__).resolve().parents[1], timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_rejects_invalid_combinations_before_dispatch(self):
        for values in ({'engine': 'other'}, {'context_mode': 'other'},
                       {'engine': 'classic', 'context_mode': 'repo'},
                       {'max_model_calls': 2}, {'max_files': True},
                       {'context_mode': 'repo', 'max_input_chars': -1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ReviewConfig(**values)

    def test_local_injection_selects_files_and_rechecks_input_before_finish(self):
        files = [FileDiff('app.py', 'first'), FileDiff('dep.py', 'second')]
        ctx, sink = context(), MemorySink()
        ctx.ensure_unchanged = Mock()
        loader = Mock(return_value=files)
        work = prepare_local_review(ReviewConfig(context_mode='repo'), paths=['dep.py'],
                                    file_loader=loader, context_factory=lambda: ctx)
        model = Mock(return_value=response())
        batch = execute_review(work, context_request_fn=model, sink=sink)
        self.assertEqual(batch.status, 'COMPLETE')
        self.assertEqual(work.detected_files, 2)
        self.assertEqual(json.loads(model.call_args.args[0][0]['content'])['git_diff'], 'second')
        self.assertEqual(ctx.ensure_unchanged.call_count, 2)
        self.assertEqual(loader.call_count, 2)
        self.assertEqual([f.path for f in sink.files], ['dep.py'])
        self.assertEqual(sink.batches, [batch])

    def test_local_changes_prevent_final_commit_but_keep_file_checkpoints(self):
        loader = Mock(side_effect=[[FileDiff('app.py', 'first')], [FileDiff('app.py', 'changed')]])
        work = prepare_local_review(ReviewConfig(context_mode='repo'),
                                    file_loader=loader, context_factory=context)
        sink = MemorySink()
        with self.assertRaisesRegex(RuntimeError, 'Local diff changed'):
            execute_review(work, context_request_fn=Mock(return_value=response()), sink=sink)
        self.assertEqual(len(sink.files), 1)
        self.assertEqual(sink.batches, [])

    def github_client(self):
        client = Mock()
        client.get_pull_request.return_value = {'state': 'open', 'draft': False,
            'head': {'sha': 'a' * 40}, 'base': {'sha': 'b' * 40}}
        client.get_pull_request_files.return_value = [{'filename': 'app.py', 'patch': '+x'}]
        return client

    def test_github_preparation_pins_version_and_service_never_publishes(self):
        client, factory = self.github_client(), Mock(return_value=context())
        work = prepare_github_review(client, 7, ReviewConfig(context_mode='repo'),
                                     expected_head_sha='a' * 40, context_factory=factory)
        factory.assert_called_once_with(client, PullRequestRevision('a' * 40, 'b' * 40))
        # Once prepared, later metadata changes must not silently refresh context.
        client.get_pull_request.return_value['head']['sha'] = 'c' * 40
        model = Mock(return_value=response())
        execute_review(work, context_request_fn=model)
        payload = json.loads(model.call_args.args[0][0]['content'])
        self.assertEqual(payload['revisions'], {'head': 'a' * 40, 'base': 'b' * 40})
        self.assertEqual(client.get_pull_request.call_count, 2)
        client.create_issue_comment.assert_not_called()
        client.update_issue_comment.assert_not_called()

    def test_stale_closed_or_draft_pr_does_not_fetch_files(self):
        for changes, expected in (({}, 'c' * 40), ({'draft': True}, None), ({'state': 'closed'}, None)):
            client = self.github_client()
            client.get_pull_request.return_value.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ReviewSupersededError):
                prepare_github_review(client, 7, ReviewConfig(), expected_head_sha=expected)
            client.get_pull_request_files.assert_not_called()

    def test_version_change_during_listing_rejects_before_context_creation(self):
        client, factory = self.github_client(), Mock()
        original = client.get_pull_request.return_value
        client.get_pull_request.side_effect = [original, {**original, 'base': {'sha': 'c' * 40}}]
        with self.assertRaises(ReviewSupersededError):
            prepare_github_review(client, 7, ReviewConfig(context_mode='repo'), context_factory=factory)
        factory.assert_not_called()

    def test_mismatched_context_or_missing_context_is_rejected_before_model(self):
        for ctx in (None, context()):
            work = PreparedReview((FileDiff('app.py', 'diff'),), ReviewConfig(context_mode='repo'),
                                  ctx, PullRequestRevision('c' * 40, 'b' * 40))
            model = Mock()
            with self.subTest(ctx=ctx), self.assertRaises(ValueError):
                execute_review(work, context_request_fn=model)
            model.assert_not_called()

    def test_all_budget_overrides_reach_graph_and_are_fresh_per_run(self):
        config = ReviewConfig(context_mode='repo', max_model_calls=1, max_tool_calls=3,
                              max_context_chars=2000, max_input_chars=30000,
                              max_files=2, max_file_chars=100, max_total_chars=200)
        work = prepared(FileDiff('app.py', 'first'), FileDiff('dep.py', 'second'), config=config)
        for _ in range(2):
            model, sink = Mock(return_value=response()), MemorySink()
            batch = execute_review(work, context_request_fn=model, sink=sink)
            self.assertEqual(batch.status, 'INCOMPLETE')
            self.assertEqual(model.call_count, 1)
            self.assertEqual([f.status for f in sink.files], ['reviewed', 'skipped'])
            event = sink.files[-1].event
            for name in ('max_model_calls', 'max_tool_calls', 'max_context_chars', 'max_input_chars',
                         'max_files', 'max_file_chars', 'max_total_chars'):
                self.assertEqual(event.limits[name], getattr(config, name))
            self.assertEqual(event.counters['model_calls'], 1)
        self.assertEqual(config.context_limits().model_calls, 0)

    def test_checkpoint_contains_validated_findings_evidence_and_one_completion_event(self):
        sink, observed = MemorySink(), []
        def model(inputs, instructions, **kwargs):
            if len(inputs) == 1:
                return response(call())
            evidence = json.loads(inputs[-1]['output'])['entries'][0]['evidence']
            return response(result={'issues': [{'severity': 'high', 'file': 'invented.py',
                'title': 'Null access', 'description': 'lookup returns None', 'evidence': [evidence]}]})
        batch = execute_review(prepared(), context_request_fn=model, sink=sink, on_record=observed.append)
        saved = sink.files[0]
        self.assertEqual(saved.status, 'reviewed')
        self.assertEqual(saved.findings[0], batch.result.issues[0])
        self.assertEqual(saved.findings[0].file, 'app.py')
        self.assertEqual(saved.findings[0].evidence[0].revision, 'a' * 40)
        self.assertEqual(saved.event.path, saved.path)
        self.assertEqual(saved.event.counters['model_calls'], 2)
        self.assertEqual(sum(e.node == 'review_file' for e in observed), 1)
        self.assertFalse(any(e.node == 'review_file' for e in sink.events))
        self.assertIsNone(next(e for e in sink.events if e.node == 'model_request').api_usage)

    def test_file_failure_skip_and_exclusion_are_saved_and_later_files_continue(self):
        sink = MemorySink()
        work = prepared(FileDiff('bad.py', 'diff'), FileDiff('good.py', 'diff'),
                        FileDiff('missing.py', '', skip_reason='unavailable patch'),
                        FileDiff('README.md', 'text'))
        model = Mock(side_effect=[TimeoutError('PRIVATE_RESPONSE'), response()])
        batch = execute_review(work, context_request_fn=model, sink=sink)
        self.assertEqual(batch.status, 'INCOMPLETE')
        self.assertEqual([f.status for f in sink.files], ['failed', 'reviewed', 'skipped', 'excluded'])
        self.assertEqual(model.call_count, 2)
        self.assertEqual(sink.files[2].reason_code, 'unavailable_diff')
        self.assertNotIn('PRIVATE_RESPONSE', repr(sink.files))

    def test_empty_and_excluded_reviews_finish_without_models(self):
        for files in ((), (FileDiff('README.md', 'text'),)):
            sink, model = MemorySink(), Mock()
            work = PreparedReview(files, ReviewConfig(context_mode='repo'), context() if files else None)
            batch = execute_review(work, context_request_fn=model, sink=sink)
            self.assertEqual(batch.status, 'NO_ELIGIBLE_FILES')
            model.assert_not_called()
            self.assertEqual(sink.batches, [batch])

    def test_classic_and_graph_diff_checkpoints_preserve_partial_results(self):
        for engine in ('classic', 'langgraph'):
            sink = MemorySink()
            finding = ReviewIssue(file='wrong.py', title='Bug', description='Failure', severity='high')
            model = Mock(side_effect=[ReviewResult(summary='', issues=[finding]), TimeoutError('timeout')])
            work = prepared(FileDiff('good.py', 'diff'), FileDiff('bad.py', 'diff'),
                FileDiff('missing.py', '', skip_reason='unavailable patch'), config=ReviewConfig(engine=engine))
            batch = execute_review(work, review_fn=model, sink=sink)
            self.assertEqual(batch.status, 'INCOMPLETE')
            self.assertEqual([f.status for f in sink.files], ['reviewed', 'failed', 'skipped'])
            self.assertEqual(sink.files[0].findings[0].file, 'good.py')
            self.assertEqual(sink.files[0].findings[0].evidence, [])

    def test_sink_mutation_cannot_corrupt_review_result_or_observer_counters(self):
        sink = MemorySink()
        def mutate(checkpoint):
            checkpoint.findings[0].title = 'MUTATED'
            checkpoint.event.counters['reviewed'] = 999
        sink.file_completed = mutate
        observed = []
        result = execute_review(prepared(config=ReviewConfig()), sink=sink, on_record=observed.append,
            review_fn=lambda diff: ReviewResult(summary='', issues=[ReviewIssue(
                file='app.py', title='Original', description='Bug', severity='high')]))
        self.assertEqual(result.result.issues[0].title, 'Original')
        self.assertEqual(next(e for e in observed if e.node == 'review_file').counters['reviewed'], 1)

    def test_ordinary_observer_failure_remains_best_effort(self):
        sink, model = MemorySink(), Mock(return_value=response())
        with self.assertWarnsRegex(RuntimeWarning, 'observation was dropped'):
            batch = execute_review(prepared(), sink=sink, context_request_fn=model,
                                   on_record=Mock(side_effect=RuntimeError('PRIVATE_OBSERVER')))
        self.assertEqual(batch.status, 'COMPLETE')
        self.assertEqual(len(sink.batches), 1)


class TestPersistenceFailures(unittest.TestCase):
    def test_checkpoint_failure_aborts_before_next_file_in_both_engines(self):
        for engine in ('classic', 'langgraph'):
            sink = MemorySink()
            sink.file_completed = Mock(side_effect=OSError('SECRET_DB_PASSWORD'))
            model = Mock(return_value=ReviewResult(summary='', issues=[]))
            work = prepared(FileDiff('one.py', 'diff'), FileDiff('two.py', 'diff'),
                            config=ReviewConfig(engine=engine))
            with self.subTest(engine=engine), self.assertRaises(ReviewPersistenceError) as caught:
                execute_review(work, review_fn=model, sink=sink)
            self.assertEqual(caught.exception.code, 'persistence_failed')
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertEqual(model.call_count, 1)
            self.assertEqual(sink.batches, [])

    def test_event_failure_before_file_prevents_any_model_dispatch(self):
        sink, model = MemorySink(), Mock()
        sink.record_event = Mock(side_effect=OSError('database offline'))
        with self.assertRaises(ReviewPersistenceError):
            execute_review(prepared(), context_request_fn=model, sink=sink)
        model.assert_not_called()
        self.assertEqual(sink.files, [])

    def test_model_event_failure_blocks_tools_repairs_and_later_files(self):
        for reply in (response(call()), response(), response(status='incomplete')):
            sink, ctx = MemorySink(), context()
            ctx.read = Mock(wraps=ctx.read)
            def fail_model_event(event):
                if event.node == 'model_request':
                    raise OSError('database offline')
            sink.record_event = fail_model_event
            model = Mock(return_value=reply)
            with self.subTest(reply=reply), self.assertRaises(ReviewPersistenceError):
                execute_review(prepared(FileDiff('one.py', 'diff'), FileDiff('two.py', 'diff'), ctx=ctx),
                               context_request_fn=model, sink=sink)
            self.assertEqual(model.call_count, 1)
            ctx.read.assert_not_called()
            self.assertEqual(sink.batches, [])

    def test_tool_event_failure_blocks_remaining_tools_in_same_response(self):
        sink, ctx = MemorySink(), context()
        ctx.read = Mock(wraps=ctx.read)
        def fail_tool(event):
            if event.node == 'read_file':
                raise OSError('database offline')
        sink.record_event = fail_tool
        model = Mock(return_value=response(call(), call(call_id='call2',
            arguments={'path': 'app.py', 'start_line': 1, 'end_line': 2})))
        with self.assertRaises(ReviewPersistenceError):
            execute_review(prepared(ctx=ctx), context_request_fn=model, sink=sink)
        self.assertEqual(ctx.read.call_count, 1)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(sink.files, [])

    def test_final_event_and_final_commit_failure_do_not_return_a_successful_batch(self):
        for failure in ('summarize', 'finish'):
            sink = MemorySink()
            if failure == 'finish':
                sink.finish = Mock(side_effect=OSError('database offline'))
            else:
                def fail_summary(event):
                    if event.node == 'summarize':
                        raise OSError('database offline')
                sink.record_event = fail_summary
            model = Mock(return_value=response())
            with self.subTest(failure=failure), self.assertRaises(ReviewPersistenceError):
                execute_review(prepared(), context_request_fn=model, sink=sink)
            self.assertEqual(len(sink.files), 1)
            self.assertEqual(sink.batches, [])
            self.assertEqual(model.call_count, 1)

    def test_failure_latch_cannot_be_cleared_by_a_recovered_sink(self):
        sink = MemorySink()
        sink.record_event = Mock(side_effect=OSError('database offline'))
        guard = PersistenceGuard(sink)
        with self.assertRaises(ReviewPersistenceError):
            guard.record_event(None)
        sink.record_event.side_effect = None
        self.assertTrue(guard.failed)
        for action in (guard.check, lambda: guard.record_event(None), lambda: guard.finish(None)):
            with self.assertRaises(ReviewPersistenceError):
                action()
        self.assertEqual(sink.record_event.call_count, 1)


if __name__ == '__main__':
    unittest.main()
