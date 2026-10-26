"""Real PostgreSQL only. Run through scripts/run_postgres_tests.py.

This directory is intentionally not a Python package, so the core CLI test
suite never imports optional database dependencies or silently skips DB tests.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import Mock
from uuid import uuid4

from alembic import command
from pydantic import ValidationError
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

from review_api.db import create_database_engine, database_url, migration_config, schema_ready, session_factory
from review_api.models import ReviewEventRecord, ReviewFile, ReviewFinding, ReviewTask
from review_api.repository import ReviewRepository, IdempotencyConflict, InvalidTransition, QueueFull, TaskNotFound
from review_api.storage_types import ReviewSubmission, StoredConfig, StoredEvent, TaskSnapshot
from review_checkpoint import FileCheckpoint, ReviewPersistenceError
from review_events import ReviewEvent
from review_models import ContextEvidence, ReviewIssue
from review_pipeline import FileProblem, finish_review_batch
from review_service import PreparedReview, ReviewConfig, execute_review
from repository_context import RepositoryContext
from diff_parser import FileDiff


HEAD, BASE = 'a' * 40, 'b' * 40


def snapshot(**overrides):
    return TaskSnapshot(head_sha=HEAD, base_sha=BASE, prompt_id='prompt-v1',
                        source_id='source-v1', model_id='mock-v1', **overrides)


def make_event(node='model_request', path='app.py', outcome='ok', reason=None, **overrides):
    values = dict(schema_version=1, timestamp=datetime.now(timezone.utc).isoformat(),
        node=node, path=path, duration_ms=1.0, outcome=outcome, termination_reason=reason,
        counters={'model_calls': 1, 'input_chars': None}, limits={'max_model_calls': 64}, api_usage=None)
    return ReviewEvent(**{**values, **overrides})


def finding(path='app.py'):
    return ReviewIssue(file=path, severity='critical', line=2, title='Null access',
        description='lookup returns None', suggestion='Check the result',
        evidence=[ContextEvidence(repository='test/repo', revision=HEAD, path='dep.py', start_line=1, end_line=2)])


def checkpoint(path='app.py', status='reviewed', findings=()):
    node = {'reviewed': 'review_file', 'failed': 'review_file', 'skipped': 'skip_file', 'excluded': 'exclude_file'}[status]
    reason = {'reviewed': None, 'failed': 'model_error', 'skipped': 'budget_exhausted', 'excluded': 'excluded'}[status]
    record = make_event(node, path, 'ok' if status == 'reviewed' else status, reason or 'completed')
    return FileCheckpoint(path, status, tuple(findings), reason, record)


def batch_for(*checkpoints):
    return finish_review_batch(issues=[issue for cp in checkpoints for issue in cp.findings],
        failures=[FileProblem(cp.path, cp.reason_code) for cp in checkpoints if cp.status == 'failed'],
        skipped=[FileProblem(cp.path, cp.reason_code) for cp in checkpoints if cp.status == 'skipped'],
        excluded=[FileProblem(cp.path, cp.reason_code) for cp in checkpoints if cp.status == 'excluded'],
        total_files=len(checkpoints), reviewed_files=sum(cp.status == 'reviewed' for cp in checkpoints))


class TestPostgresStorage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = database_url(os.environ.get('REVIEW_TEST_DATABASE_URL', ''))
        marker = os.environ.get('REVIEW_ISOLATED_TEST_DB', '')
        if url.database != marker or not re.fullmatch(r'review_test_[0-9a-f]{32}', marker):
            raise RuntimeError('Use scripts/run_postgres_tests.py; an isolated test database is required')
        cls.engine = create_database_engine(url)
        cls.sessions = session_factory(cls.engine)
        # The runner creates a fresh database; another integration class may
        # have already applied this same migration before this class runs.
        with cls.engine.begin() as connection:
            command.upgrade(migration_config(connection), 'head')
        cls.repository = ReviewRepository(cls.sessions, waiting_limit=8)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        # The runner created this database just for this invocation; never a dev volume.
        with self.engine.begin() as connection:
            connection.execute(text('TRUNCATE review_tasks CASCADE'))

    def submit(self, number=1, key=None):
        return self.repository.submit(key or uuid4(), ReviewSubmission(owner='test', repo='repo', pr_number=number), snapshot())[0]

    def running(self):
        task = self.submit()
        claimed = self.repository.claim_next()
        self.assertEqual(claimed['id'], task['id'])
        return task['id']

    def counts(self):
        with self.sessions() as session:
            return tuple(session.scalar(select(func.count()).select_from(model))
                         for model in (ReviewTask, ReviewFile, ReviewFinding, ReviewEventRecord))

    def test_migration_repeatable_and_metadata_has_no_drift(self):
        with self.engine.begin() as connection:
            command.upgrade(migration_config(connection), 'head')
            command.check(migration_config(connection))
        self.assertTrue(schema_ready(self.engine))
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_concurrent_same_key_creates_one_task(self):
        key, barrier = uuid4(), threading.Barrier(6)
        request = ReviewSubmission(owner='TEST', repo='Repo', pr_number=1)
        def submit(_):
            barrier.wait(timeout=10)
            return self.repository.submit(key, request, snapshot())
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(submit, range(6)))
        self.assertEqual(len({item[0]['id'] for item in results}), 1)
        self.assertEqual(sum(item[1] for item in results), 1)
        self.assertEqual(self.counts()[0], 1)
        self.assertEqual(results[0][0]['owner'], 'test')

    def test_replay_uses_client_fingerprint_not_new_head_or_config(self):
        key = uuid4()
        request = ReviewSubmission(owner='TEST', repo='repo', pr_number=1, expected_head_sha=HEAD)
        old, _ = self.repository.submit(key, request, snapshot())
        changed = snapshot().model_copy(update={'head_sha': 'c' * 40, 'model_id': 'changed'})
        actual, created = self.repository.submit(key, request, changed)
        self.assertFalse(created)
        self.assertEqual(actual['id'], old['id'])
        self.assertEqual(actual['head_sha'], HEAD)
        normalized = ReviewSubmission(owner='test', repo='REPO', pr_number=1, expected_head_sha=HEAD)
        self.assertEqual(self.repository.find_replay(key, normalized)['id'], old['id'])
        for changes in ({'pr_number': 2}, {'profile': 'other'}, {'expected_head_sha': None}):
            with self.subTest(changes=changes), self.assertRaises(IdempotencyConflict):
                self.repository.submit(key, request.model_copy(update=changes), snapshot())

    def test_expected_revision_conflict_creates_no_row(self):
        with self.assertRaises(IdempotencyConflict):
            self.repository.submit(uuid4(), ReviewSubmission(owner='test', repo='repo', pr_number=1,
                expected_head_sha='c' * 40), snapshot())
        self.assertEqual(self.counts()[0], 0)

    def test_capacity_is_serialized_and_replay_works_when_full(self):
        repo = ReviewRepository(self.sessions, waiting_limit=2)
        barrier = threading.Barrier(5)
        def submit(number):
            barrier.wait(timeout=10)
            try:
                return repo.submit(uuid4(), ReviewSubmission(owner='test', repo='repo', pr_number=number + 1), snapshot())
            except QueueFull:
                return None
        with ThreadPoolExecutor(max_workers=5) as pool:
            rows = list(pool.map(submit, range(5)))
        self.assertEqual(sum(row is not None for row in rows), 2)
        self.assertEqual(self.counts()[0], 2)
        with self.sessions() as session:
            first = session.scalar(select(ReviewTask).limit(1))
            key, number = first.idempotency_key, first.pr_number
        _, created = repo.submit(key, ReviewSubmission(owner='test', repo='repo', pr_number=number), snapshot())
        self.assertFalse(created)

    def test_concurrent_claim_has_one_active_task(self):
        for i in range(3):
            self.submit(i + 1)
        barrier = threading.Barrier(4)
        def claim(_):
            barrier.wait(timeout=10)
            return self.repository.claim_next()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(claim, range(4)))
        self.assertEqual(sum(row is not None for row in results), 1)
        task = next(row for row in results if row)
        self.repository.terminate(task['id'], status='interrupted', error_code='shutdown')
        self.assertIsNotNone(self.repository.claim_next())

    def test_complete_incomplete_and_no_eligible_are_distinct(self):
        cases = [('COMPLETE', [checkpoint(findings=[finding()])]),
                 ('INCOMPLETE', [checkpoint(), checkpoint('bad.py', 'failed'), checkpoint('skip.py', 'skipped')]),
                 ('NO_ELIGIBLE_FILES', [checkpoint('README.md', 'excluded')]),
                 ('NO_ELIGIBLE_FILES', [])]
        for expected, files in cases:
            task_id = self.running()
            for cp in files:
                self.repository.save_file(task_id, cp)
            final = self.repository.finish(task_id, batch_for(*files))
            self.assertEqual(final['review_status'], expected)
            self.assertFalse(final['partial'])
            self.assertEqual(final['publication_state'], 'disabled')
            result = self.repository.get_result(task_id)
            self.assertEqual(len(result['files']), len(files))
            self.assertEqual(result['head_sha'], HEAD)

    def test_partial_results_survive_failure_and_connection_recreation(self):
        task_id = self.running()
        cp = checkpoint(findings=[finding()])
        self.repository.save_file(task_id, cp)
        self.assertTrue(self.repository.get_result(task_id)['partial'])
        self.repository.terminate(task_id, status='interrupted', error_code='shutdown')
        self.engine.dispose()
        result = self.repository.get_result(task_id)
        self.assertTrue(result['partial'])
        self.assertIsNone(result['review_status'])
        self.assertEqual(result['files'][0]['findings'][0]['evidence'][0]['revision'], HEAD)
        self.assertFalse(self.repository.get_task(task_id)['resource_records_complete'])

    def test_file_findings_and_event_are_atomic_on_event_insert_failure(self):
        task_id = self.running()
        def fail_insert(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('INSERT INTO review_events'):
                raise RuntimeError('injected event failure')
        event.listen(self.engine, 'before_cursor_execute', fail_insert)
        try:
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                self.repository.save_file(task_id, checkpoint(findings=[finding()]))
        finally:
            event.remove(self.engine, 'before_cursor_execute', fail_insert)
        self.assertEqual(self.counts(), (1, 0, 0, 0))
        self.assertEqual(self.repository.get_task(task_id)['last_event_seq'], 0)

    def test_final_commit_failure_keeps_running_task_and_saved_files(self):
        task_id = self.running()
        cp = checkpoint()
        self.repository.save_file(task_id, cp)
        def fail_update(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('UPDATE review_tasks'):
                raise RuntimeError('injected final update failure')
        event.listen(self.engine, 'before_cursor_execute', fail_update)
        try:
            with self.assertRaises(RuntimeError):
                self.repository.finish(task_id, batch_for(cp))
        finally:
            event.remove(self.engine, 'before_cursor_execute', fail_update)
        task = self.repository.get_task(task_id)
        self.assertEqual(task['execution_status'], 'running')
        self.assertIsNone(task['summary'])
        self.assertEqual(self.counts(), (1, 1, 0, 1))

    def test_mismatched_final_batch_is_rejected(self):
        task_id = self.running()
        self.repository.save_file(task_id, checkpoint())
        with self.assertRaises(ValueError):
            self.repository.finish(task_id, batch_for())
        self.assertEqual(self.repository.get_task(task_id)['execution_status'], 'running')

    def test_duplicate_file_and_event_sequences_rejected_without_partial_writes(self):
        task_id = self.running()
        cp = checkpoint(findings=[finding()])
        self.repository.save_file(task_id, cp)
        with self.assertRaises(IntegrityError):
            self.repository.save_file(task_id, cp)
        with self.assertRaises(IntegrityError), self.sessions.begin() as session:
            session.add(ReviewEventRecord(task_id=task_id, seq=1, **StoredEvent.from_event(make_event()).model_dump()))
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertEqual(self.repository.get_task(task_id)['last_event_seq'], 1)

    def test_illegal_transitions_and_snapshot_changes_rejected_by_database(self):
        task_id = self.running()
        for sql in ("UPDATE review_tasks SET execution_status='queued', started_at=NULL WHERE id=:id",
                    "UPDATE review_tasks SET head_sha=:sha WHERE id=:id",
                    "UPDATE review_tasks SET publication_state='published' WHERE id=:id"):
            with self.subTest(sql=sql), self.assertRaises(IntegrityError), self.engine.begin() as connection:
                connection.execute(text(sql), {'id': task_id, 'sha': 'c' * 40})
        self.repository.finish(task_id, batch_for())
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(text("UPDATE review_tasks SET execution_status='running', review_status=NULL, finished_at=NULL, summary=NULL, total_files=NULL, reviewed_files=NULL, failed_files=NULL, skipped_files=NULL, excluded_files=NULL WHERE id=:id"), {'id': task_id})
        with self.assertRaises(InvalidTransition):
            self.repository.append_event(task_id, make_event())

    def test_unique_active_task_constraint_applies_outside_repository(self):
        self.running()
        waiting = self.submit(2)
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(text("UPDATE review_tasks SET execution_status='running', started_at=now() WHERE id=:id"), {'id': waiting['id']})

    def test_event_sequence_concurrency_pagination_and_unknown_usage(self):
        task_id = self.running()
        with ThreadPoolExecutor(max_workers=4) as pool:
            seqs = list(pool.map(lambda _: self.repository.append_event(task_id, make_event()), range(12)))
        self.assertEqual(sorted(seqs), list(range(1, 13)))
        first = self.repository.list_events(task_id, limit=5)
        self.assertTrue(first['has_more'])
        self.assertEqual(first['next_after_seq'], 5)
        second = self.repository.list_events(task_id, after_seq=5, limit=7)
        self.assertEqual([e['seq'] for e in second['items']], list(range(6, 13)))
        self.assertFalse(second['has_more'])
        self.assertIsNone(second['items'][0]['api_usage'])
        self.assertIsNone(second['items'][0]['counters']['input_chars'])

    def test_task_cursor_handles_tied_timestamps_and_new_insert(self):
        # One transaction gives identical server timestamps to all inserted rows.
        with self.sessions.begin() as session:
            for number in range(5):
                request = ReviewSubmission(owner='test', repo='repo', pr_number=number + 1)
                session.add(ReviewTask(id=uuid4(), idempotency_key=uuid4(), request_fingerprint=request.fingerprint(),
                    owner='test', repo='repo', pr_number=number + 1, profile='default',
                    head_sha=HEAD, base_sha=BASE, config=StoredConfig().model_dump(),
                    prompt_id='p', source_id='s', model_id='m'))
        first = self.repository.list_tasks(limit=2)
        new = self.submit(99)
        ids = [row['id'] for row in first.items]
        cursor = first.next_cursor
        while cursor:
            page = self.repository.list_tasks(limit=2, cursor=cursor)
            ids += [row['id'] for row in page.items]
            cursor = page.next_cursor
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)
        self.assertNotIn(new['id'], ids)

    def test_invalid_limits_cursors_missing_tasks_and_unknown_fields(self):
        for kwargs in ({'limit': 0}, {'limit': 101}, {'cursor': 'garbage'}, {'cursor': 'x' * 300}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.repository.list_tasks(**kwargs)
        with self.assertRaises(ValueError):
            self.repository.list_events(uuid4(), limit=501)
        with self.assertRaises(TaskNotFound):
            self.repository.get_result(uuid4())
        with self.assertRaises(ValidationError):
            StoredConfig(api_key='DO_NOT_STORE')
        with self.assertRaises(ValidationError):
            self.repository.append_event(self.running(), make_event(counters={'raw_response': 1}))

    def test_bad_checkpoint_or_wrong_revision_cannot_persist(self):
        task_id = self.running()
        cp = checkpoint(findings=[finding()])
        for bad in (replace(cp, path='../bad.py'), replace(cp, event=make_event()),
                    replace(cp, status='failed'), replace(cp, path='x' * 513)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.repository.save_file(task_id, bad)
        bad_issue = finding()
        bad_issue.evidence[0].revision = 'c' * 40
        with self.assertRaises(ValueError):
            self.repository.save_file(task_id, checkpoint(findings=[bad_issue]))
        missing_evidence = finding()
        missing_evidence.evidence = []
        with self.assertRaises(ValueError):
            self.repository.save_file(task_id, checkpoint(findings=[missing_evidence]))
        self.assertEqual(self.counts(), (1, 0, 0, 0))

    def test_finding_unique_ordinal_and_foreign_key_are_database_constraints(self):
        task_id = self.running()
        self.repository.save_file(task_id, checkpoint(findings=[finding()]))
        with self.sessions() as session:
            file_id = session.scalar(select(ReviewFile.id))
        values = dict(ordinal=0, title='duplicate', body='duplicate', severity='high', evidence=[])
        for identity in (file_id, uuid4()):
            with self.subTest(identity=identity), self.assertRaises(IntegrityError), self.sessions.begin() as session:
                session.add(ReviewFinding(file_id=identity, **values))
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_queued_to_finished_and_invalid_event_sequence_are_rejected_by_database(self):
        task_id = self.submit()['id']
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(text("UPDATE review_tasks SET execution_status='finished', review_status='NO_ELIGIBLE_FILES', started_at=now(), finished_at=now(), summary='empty', total_files=0, reviewed_files=0, failed_files=0, skipped_files=0, excluded_files=0 WHERE id=:id"), {'id': task_id})
        with self.assertRaises(IntegrityError), self.sessions.begin() as session:
            session.add(ReviewEventRecord(task_id=task_id, seq=0, **StoredEvent.from_event(make_event()).model_dump()))
        self.assertEqual(self.counts(), (1, 0, 0, 0))

    def test_service_to_real_database_round_trip_with_mock_model(self):
        task_id = self.running()
        repo = self.repository
        class Sink:
            def record_event(self, value):
                repo.append_event(task_id, value)
            def file_completed(self, value):
                repo.save_file(task_id, value)
            def finish(self, value):
                repo.finish(task_id, value)
        from types import SimpleNamespace
        model = Mock(return_value=SimpleNamespace(status='completed', output=[],
            output_text=json.dumps({'summary': '', 'issues': [], 'context_complete': True, 'context_reason': ''})))
        ctx = RepositoryContext('test/repo', {'head': HEAD, 'base': BASE}, {'head': {}, 'base': {}})
        work = PreparedReview((FileDiff('app.py', '+x'),), ReviewConfig(context_mode='repo'), ctx)
        result = execute_review(work, context_request_fn=model, sink=Sink())
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(model.call_count, 1)
        task = repo.get_task(task_id)
        self.assertEqual(task['execution_status'], 'finished')
        self.assertEqual(task['review_status'], 'COMPLETE')
        self.assertEqual(len(repo.get_result(task_id)['files']), 1)

    def test_database_write_failure_stops_service_before_second_model_call(self):
        task_id, repo = self.running(), self.repository
        class Sink:
            def record_event(self, value):
                repo.append_event(task_id, value)
            def file_completed(self, value):
                repo.save_file(task_id, value)
            def finish(self, value):
                repo.finish(task_id, value)
        from types import SimpleNamespace
        model = Mock(return_value=SimpleNamespace(status='completed', output=[],
            output_text=json.dumps({'summary': '', 'issues': [], 'context_complete': True, 'context_reason': ''})))
        ctx = RepositoryContext('test/repo', {'head': HEAD, 'base': BASE}, {'head': {}, 'base': {}})
        work = PreparedReview((FileDiff('app.py', '+x'), FileDiff('next.py', '+y')), ReviewConfig(context_mode='repo'), ctx)
        def fail_file(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('INSERT INTO review_files'):
                raise RuntimeError('injected database failure')
        event.listen(self.engine, 'before_cursor_execute', fail_file)
        try:
            with self.assertRaises(ReviewPersistenceError):
                execute_review(work, context_request_fn=model, sink=Sink())
        finally:
            event.remove(self.engine, 'before_cursor_execute', fail_file)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(repo.get_task(task_id)['execution_status'], 'running')
        self.assertEqual(repo.get_result(task_id)['files'], [])


class TestTestEnvironmentSafety(unittest.TestCase):
    def test_runner_refuses_development_or_remote_databases(self):
        from scripts.run_postgres_tests import validate_test_admin
        for url in ('postgresql+psycopg://u:p@127.0.0.1/development',
                    'postgresql+psycopg://u:p@remote/review_test_admin',
                    'postgresql+psycopg://u:p@localhost/review_test_admin?dbname=production',
                    'postgresql+psycopg://u:@localhost/review_test_admin'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_test_admin(url)

    def test_fastapi_testclient_uses_real_compatible_httpx(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        @app.get('/dependency-check')
        def check():
            return {'ok': True}
        with TestClient(app) as client:
            self.assertEqual(client.get('/dependency-check').json(), {'ok': True})
