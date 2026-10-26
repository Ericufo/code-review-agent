"""Real PostgreSQL lifecycle/failure tests with deterministic repository/model fixtures."""
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import re
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from alembic import command
from sqlalchemy import event, text

from diff_parser import FileDiff
from github_review import PullRequestRevision
from repository_context import RepositoryContext
from review_service import PreparedReview, ReviewConfig
from review_api.db import create_database_engine, database_url, migration_config, session_factory
from review_api.executor import ExecutorNotReady, ReviewExecutor, mock_context_request
from review_api.repository import ReviewRepository
from review_api.storage_types import ReviewSubmission, StoredConfig
from test_storage import checkpoint, snapshot


def fixture_provider(task, before_work, *, files=None):
    before_work()
    config = ReviewConfig(**StoredConfig.model_validate(task['config']).model_dump(exclude={'model_mode'}))
    revision = PullRequestRevision(task['head_sha'], task['base_sha'])
    context = RepositoryContext(f"{task['owner']}/{task['repo']}",
        {'head': task['head_sha'], 'base': task['base_sha']},
        {'head': {'app.py': 'x = 1\n', 'next.py': 'y = 2\n'}, 'base': {}})
    return PreparedReview(tuple(files if files is not None else [FileDiff('app.py', '+x')]),
                          config, context, revision)


def tool_response():
    call = {'type': 'function_call', 'name': 'read_file', 'call_id': 'read1',
            'arguments': json.dumps({'path': 'app.py', 'start_line': 1, 'end_line': 1})}
    return SimpleNamespace(status='completed', usage=None, output_text='',
                           output=[SimpleNamespace(model_dump=lambda **kwargs: dict(call))])


def crash_child():
    """A real process is killed after file one is committed and file two starts."""
    engine = create_database_engine(os.environ['REVIEW_TEST_DATABASE_URL'])
    count = 0
    def model(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            threading.Event().wait(60)
        return mock_context_request(*args, **kwargs)
    executor = ReviewExecutor(engine, lambda task, check: fixture_provider(task, check,
        files=[FileDiff('app.py', '+x'), FileDiff('next.py', '+y')]), mock_request_fn=model)
    if not executor.start():
        raise SystemExit(2)
    executor.submit(uuid4(), ReviewSubmission(owner='test', repo='repo', pr_number=1), snapshot())
    threading.Event().wait(60)


class TestExecutor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = database_url(os.environ.get('REVIEW_TEST_DATABASE_URL', ''))
        marker = os.environ.get('REVIEW_ISOLATED_TEST_DB', '')
        if url.database != marker or not re.fullmatch(r'review_test_[0-9a-f]{32}', marker):
            raise RuntimeError('Executor tests require the isolated PostgreSQL runner')
        cls.engine = create_database_engine(url)
        cls.sessions = session_factory(cls.engine)
        with cls.engine.begin() as connection:
            command.upgrade(migration_config(connection), 'head')

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.executors, self.releases = [], []
        self.repository = ReviewRepository(self.sessions)
        with self.engine.begin() as connection:
            connection.execute(text('TRUNCATE review_tasks CASCADE'))

    def tearDown(self):
        for release in self.releases:
            release.set()
        for executor in reversed(self.executors):
            self.assertTrue(executor.stop(grace=5), 'Worker did not stop during test cleanup')

    def executor(self, **kwargs):
        prepare = kwargs.pop('prepare', fixture_provider)
        executor = ReviewExecutor(self.engine, prepare, poll_interval=0.02, **kwargs)
        self.executors.append(executor)
        return executor

    def submit(self, executor=None, *, number=1, config=None):
        target = executor or self.repository
        return target.submit(uuid4(), ReviewSubmission(owner='test', repo='repo', pr_number=number),
                             snapshot(**({'config': config} if config else {})))[0]['id']

    def wait(self, predicate, *, timeout=6):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail('Timed out waiting for executor state')

    def terminal(self, task_id):
        return self.wait(lambda: self.repository.get_task(task_id)['execution_status']
                         if self.repository.get_task(task_id)['execution_status'] in {'finished', 'failed', 'interrupted'} else None)

    def blocking_model(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def model(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test model release timeout')
            return mock_context_request(*args, **kwargs)
        return Mock(side_effect=model), entered, release

    def test_default_mock_task_finishes_without_using_cli_live_model(self):
        with patch('context_review.request_context_model', side_effect=AssertionError('live model forbidden')):
            worker = self.executor()
            self.assertTrue(worker.start())
            task_id = self.submit(worker)
            self.assertEqual(self.terminal(task_id), 'finished')
        task = self.repository.get_task(task_id)
        self.assertEqual(task['config']['model_mode'], 'mock')
        self.assertEqual(task['review_status'], 'COMPLETE')
        events = self.repository.list_events(task_id)['items']
        self.assertEqual([e['seq'] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(sum(e['node'] == 'review_file' for e in events), 1)
        self.assertIsNone(next(e for e in events if e['node'] == 'model_request')['api_usage'])

    def test_lost_notification_is_recovered_by_polling(self):
        worker = self.executor()
        self.assertTrue(worker.start())
        # Bypass notify entirely; committed queued rows remain authoritative.
        task_id = self.submit()
        self.assertEqual(self.terminal(task_id), 'finished')

    def test_second_executor_refuses_ready_without_touching_running_or_queued_tasks(self):
        model, entered, release = self.blocking_model()
        first = self.executor(mock_request_fn=model)
        self.assertTrue(first.start())
        running = self.submit(first)
        self.assertTrue(entered.wait(4))
        queued = self.submit(first, number=2)
        second = self.executor()
        self.assertFalse(second.start())
        self.assertEqual(second.failure_code, 'executor_busy')
        self.assertEqual(self.repository.get_task(running)['execution_status'], 'running')
        self.assertEqual(self.repository.get_task(queued)['execution_status'], 'queued')
        release.set()
        self.assertEqual(self.terminal(running), 'finished')
        self.assertEqual(self.terminal(queued), 'finished')
        self.assertEqual(model.call_count, 2)

    def test_startup_interrupts_stale_rows_preserving_results_without_model_calls(self):
        running = self.submit()
        self.repository.claim_next()
        self.repository.save_file(running, checkpoint())
        queued = self.submit(number=2)
        model = Mock(side_effect=AssertionError('old work must not run'))
        worker = self.executor(mock_request_fn=model)
        self.assertTrue(worker.start())
        for identity in (running, queued):
            task = self.repository.get_task(identity)
            self.assertEqual(task['execution_status'], 'interrupted')
            self.assertTrue(task['partial'])
            self.assertFalse(task['resource_records_complete'])
        self.assertEqual(len(self.repository.get_result(running)['files']), 1)
        model.assert_not_called()

    def test_request_shutdown_saves_inflight_file_then_interrupts_remaining_files(self):
        model, entered, release = self.blocking_model()
        worker = self.executor(mock_request_fn=model, prepare=lambda task, check: fixture_provider(task, check,
            files=[FileDiff('app.py', '+x'), FileDiff('next.py', '+y')]))
        self.assertTrue(worker.start())
        task_id = self.submit(worker)
        self.assertTrue(entered.wait(4))
        queued = self.submit(worker, number=2)
        worker.request_stop()
        with self.assertRaises(ExecutorNotReady):
            self.submit(worker, number=3)
        release.set()
        self.assertTrue(worker.stop(grace=4))
        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.repository.get_task(task_id)['execution_status'], 'interrupted')
        self.assertEqual(len(self.repository.get_result(task_id)['files']), 1)
        self.assertEqual(self.repository.get_task(queued)['execution_status'], 'interrupted')

    def test_last_inflight_file_can_finish_within_grace(self):
        model, entered, release = self.blocking_model()
        worker = self.executor(mock_request_fn=model)
        self.assertTrue(worker.start())
        task_id = self.submit(worker)
        self.assertTrue(entered.wait(4))
        worker.request_stop()
        release.set()
        self.assertTrue(worker.stop(grace=4))
        self.assertEqual(self.repository.get_task(task_id)['execution_status'], 'finished')

    def test_shutdown_deadline_returns_without_unlocking_or_accepting_late_result(self):
        model, entered, release = self.blocking_model()
        worker = self.executor(mock_request_fn=model)
        self.assertTrue(worker.start())
        task_id = self.submit(worker)
        self.assertTrue(entered.wait(4))
        started = time.monotonic()
        self.assertFalse(worker.stop(grace=0.02))
        self.assertLess(time.monotonic() - started, 0.8)
        second = self.executor()
        self.assertFalse(second.start())
        self.assertEqual(second.failure_code, 'executor_busy')
        release.set()
        self.assertTrue(worker.stop(grace=4))
        task = self.repository.get_task(task_id)
        self.assertEqual(task['execution_status'], 'interrupted')
        self.assertFalse(task['resource_records_complete'])
        self.assertEqual(self.repository.get_result(task_id)['files'], [])

    def test_persistence_event_failure_stops_worker_and_blocks_further_dispatch(self):
        def fail_event(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('INSERT INTO review_events'):
                raise RuntimeError('PRIVATE_DATABASE_BODY')
        model = Mock(return_value=mock_context_request([], ''))
        worker = self.executor(mock_request_fn=model)
        self.assertTrue(worker.start())
        event.listen(self.engine, 'before_cursor_execute', fail_event)
        try:
            task_id = self.submit(worker)
            self.assertEqual(self.terminal(task_id), 'failed')
            self.assertTrue(worker.stop(grace=4))
        finally:
            event.remove(self.engine, 'before_cursor_execute', fail_event)
        model.assert_not_called()
        self.assertFalse(worker.ready)
        self.assertEqual(worker.failure_code, 'persistence_failed')
        task = self.repository.get_task(task_id)
        self.assertEqual(task['error_code'], 'persistence_failed')
        self.assertFalse(task['resource_records_complete'])
        self.assertNotIn('PRIVATE', repr(task))

    def test_final_summary_commit_failure_preserves_files_and_never_reports_finished(self):
        repository = ReviewRepository(self.sessions)
        repository.finish = Mock(side_effect=RuntimeError('PRIVATE_FINAL_ERROR'))
        worker = self.executor(repository=repository)
        self.assertTrue(worker.start())
        task_id = self.submit(worker)
        self.assertEqual(self.terminal(task_id), 'failed')
        self.assertTrue(worker.stop(grace=4))
        result = self.repository.get_result(task_id)
        self.assertTrue(result['partial'])
        self.assertIsNone(result['review_status'])
        self.assertEqual(len(result['files']), 1)

    def test_model_event_failure_prevents_tool_dispatch_and_further_model_calls(self):
        repository = ReviewRepository(self.sessions)
        original = repository.append_event
        def append(task_id, record):
            if record.node == 'model_request':
                raise RuntimeError('PRIVATE_EVENT_ERROR')
            return original(task_id, record)
        repository.append_event = append
        contexts = []
        def prepare(task, check):
            value = fixture_provider(task, check)
            value.context.read = Mock(wraps=value.context.read)
            contexts.append(value.context)
            return value
        model = Mock(return_value=tool_response())
        worker = self.executor(repository=repository, prepare=prepare, mock_request_fn=model)
        self.assertTrue(worker.start())
        task_id = self.submit(worker)
        self.assertEqual(self.terminal(task_id), 'failed')
        self.assertTrue(worker.stop(grace=4))
        self.assertEqual(worker.failure_code, 'persistence_failed')
        contexts[0].read.assert_not_called()
        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.repository.get_result(task_id)['files'], [])

    def test_shutdown_blocks_tools_and_format_repairs_from_inflight_response(self):
        tool_reply = tool_response()
        bad_reply = SimpleNamespace(status='completed', usage=None, output_text='invalid json', output=[])
        for reply in (tool_reply, bad_reply):
            with self.subTest(reply=reply):
                contexts = []
                def prepare(task, check):
                    value = fixture_provider(task, check)
                    value.context.read = Mock(wraps=value.context.read)
                    contexts.append(value.context)
                    return value
                def model(*args, **kwargs):
                    worker.request_stop()
                    return reply
                request = Mock(side_effect=model)
                worker = self.executor(prepare=prepare, mock_request_fn=request)
                self.assertTrue(worker.start())
                task_id = self.submit(worker)
                self.assertEqual(self.terminal(task_id), 'interrupted')
                self.assertTrue(worker.stop(grace=4))
                contexts[0].read.assert_not_called()
                self.assertEqual(request.call_count, 1)
                self.assertIsNone(worker.failure_code)

    def test_file_failure_and_empty_review_are_successful_execution_with_distinct_coverage(self):
        for files, expected in (([FileDiff('app.py', '+x'), FileDiff('next.py', '+y')], 'INCOMPLETE'),
                                ([], 'NO_ELIGIBLE_FILES')):
            with self.subTest(expected=expected):
                model = Mock(side_effect=[TimeoutError('PRIVATE_MODEL_ERROR'), mock_context_request([], '')])
                worker = self.executor(mock_request_fn=model,
                    prepare=lambda task, check: fixture_provider(task, check, files=files))
                self.assertTrue(worker.start())
                task_id = self.submit(worker)
                self.assertEqual(self.terminal(task_id), 'finished')
                self.assertTrue(worker.stop(grace=4))
                result = self.repository.get_result(task_id)
                self.assertEqual(result['review_status'], expected)
                self.assertEqual(model.call_count, len(files))
                self.assertFalse(result['partial'])
                self.assertNotIn('PRIVATE_MODEL_ERROR', repr(result))

    def test_killed_lock_session_is_not_reconnected_and_unknown_usage_is_not_zero(self):
        model, entered, release = self.blocking_model()
        worker = self.executor(mock_request_fn=model)
        self.assertTrue(worker.start())
        task_id = self.submit(worker)
        self.assertTrue(entered.wait(4))
        pid = worker.lock_backend_pid
        with self.engine.begin() as connection:
            self.assertTrue(connection.scalar(text('SELECT pg_terminate_backend(:pid)'), {'pid': pid}))
        release.set()
        self.assertEqual(self.terminal(task_id), 'failed')
        self.assertTrue(worker.stop(grace=4))
        self.assertEqual(worker.failure_code, 'executor_lost')
        self.assertEqual(worker.lock_backend_pid, pid)
        self.assertEqual(model.call_count, 1)
        task = self.repository.get_task(task_id)
        self.assertFalse(task['resource_records_complete'])
        self.assertEqual(task['error_code'], 'executor_lost')
        self.assertFalse(any(e['node'] == 'model_request' for e in self.repository.list_events(task_id)['items']))

    def test_pinned_version_or_config_mismatch_fails_before_model_and_worker_remains_usable(self):
        model = Mock(return_value=mock_context_request([], ''))
        def wrong(task, check):
            value = fixture_provider(task, check)
            return replace(value, revision=PullRequestRevision('c' * 40, task['base_sha']))
        worker = self.executor(prepare=wrong, mock_request_fn=model)
        self.assertTrue(worker.start())
        task_id = self.submit(worker)
        self.assertEqual(self.terminal(task_id), 'failed')
        model.assert_not_called()
        self.assertTrue(worker.ready)
        self.assertEqual(self.repository.get_task(task_id)['error_code'], 'service_failed')

    def test_live_mode_does_not_fall_back_to_environment_credentials(self):
        provider = Mock(side_effect=AssertionError('provider must not run'))
        worker = self.executor(prepare=provider)
        self.assertTrue(worker.start())
        task_id = self.submit(worker, config=StoredConfig(model_mode='live'))
        self.assertEqual(self.terminal(task_id), 'failed')
        provider.assert_not_called()
        with self.assertRaises(ValueError):
            self.executor(allow_live=True)

    def test_repeated_notifications_do_not_duplicate_execution(self):
        model = Mock(return_value=mock_context_request([], ''))
        worker = self.executor(mock_request_fn=model)
        self.assertTrue(worker.start())
        task_id = self.submit(worker)
        for _ in range(40):
            worker.notify()
        self.assertEqual(self.terminal(task_id), 'finished')
        self.assertTrue(worker.stop(grace=4))
        self.assertEqual(model.call_count, 1)

    def test_real_process_crash_preserves_completed_file_and_restart_never_resumes(self):
        root = Path(__file__).resolve().parents[2]
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(root), str(Path(__file__).parent)])}
        child = subprocess.Popen([sys.executable, '-c', 'from test_executor import crash_child; crash_child()'],
            cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            def first_saved():
                rows = self.repository.list_tasks().items
                if not rows:
                    return None
                task_id = rows[0]['id']
                return task_id if len(self.repository.get_result(task_id)['files']) == 1 else None
            task_id = self.wait(first_saved, timeout=10)
            child.terminate()
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stderr.close()
        model = Mock(side_effect=AssertionError('crashed work must not resume'))
        worker = self.executor(mock_request_fn=model)
        self.assertTrue(worker.start())
        task = self.repository.get_task(task_id)
        self.assertEqual(task['execution_status'], 'interrupted')
        self.assertFalse(task['resource_records_complete'])
        self.assertEqual(len(self.repository.get_result(task_id)['files']), 1)
        model.assert_not_called()

    def test_schema_mismatch_refuses_ready_without_mutating_existing_tasks(self):
        task_id = self.submit()
        worker = self.executor()
        with patch('review_api.executor.schema_ready', return_value=False):
            self.assertFalse(worker.start())
            self.assertTrue(worker.stop(grace=4))
        self.assertEqual(worker.failure_code, 'schema_mismatch')
        self.assertEqual(self.repository.get_task(task_id)['execution_status'], 'queued')
