"""HTTP + real PostgreSQL + real executor; only upstream repository/model are fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import os
import re
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from diff_parser import FileDiff
from github_client import GitHubAPIError
from github_review import PullRequestRevision, ReviewSupersededError
from review_api.app import create_app
from review_api.db import create_database_engine, database_url, migration_config, session_factory
from review_api.executor import mock_context_request
from review_api.providers import UpstreamTimeout
from review_api.repository import ReviewRepository
from review_api.settings import Settings
from review_api.storage_types import ReviewSubmission, StoredConfig
from test_executor import fixture_provider
from test_storage import HEAD, BASE, checkpoint, finding, snapshot


TOKEN = 'test-only-review-token'
AUTH = {'Authorization': 'Bearer ' + TOKEN}
BODY = {'owner': 'Test', 'repo': 'Repo', 'pr_number': 1}


class TestHTTPAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = database_url(os.environ.get('REVIEW_TEST_DATABASE_URL', ''))
        marker = os.environ.get('REVIEW_ISOLATED_TEST_DB', '')
        if url.database != marker or not re.fullmatch(r'review_test_[0-9a-f]{32}', marker):
            raise RuntimeError('Use the isolated PostgreSQL runner')
        cls.engine = create_database_engine(url)
        with cls.engine.begin() as connection:
            command.upgrade(migration_config(connection), 'head')

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text('TRUNCATE review_tasks CASCADE'))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('review_api.providers.requests.get', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('review_api.providers.OpenAI', side_effect=AssertionError('live model forbidden')))
        self.publish = self.stack.enter_context(patch('github_client.GitHubClient.create_issue_comment', side_effect=AssertionError('publication forbidden')))
        self.stack.enter_context(patch('github_client.GitHubClient.update_issue_comment', side_effect=AssertionError('publication forbidden')))
        self.settings = Settings(database_url=self.engine.url.render_as_string(hide_password=False),
                                 api_token=TOKEN, allowed_repos={'test/repo'})
        self.provider = Mock()
        self.provider.metadata.return_value = PullRequestRevision(HEAD, BASE)
        self.provider.prepare.side_effect = fixture_provider

    def client(self, *, settings=None, provider=None, model=mock_context_request):
        app = create_app(settings or self.settings, engine=self.engine, provider=provider or self.provider,
                         mock_request_fn=model)
        return TestClient(app, headers=AUTH)

    def submit(self, client, *, key=None, body=None):
        return client.post('/api/v1/reviews', json=body or BODY,
                           headers={'Idempotency-Key': key or str(uuid4())})

    def wait(self, client, task_id):
        end = time.monotonic() + 6
        while time.monotonic() < end:
            response = client.get('/api/v1/reviews/' + task_id)
            self.assertEqual(response.status_code, 200, response.text)
            if response.json()['execution_status'] in {'finished', 'failed', 'interrupted'}:
                return response.json()
            time.sleep(.02)
        self.fail('Task did not finish')

    def assert_error(self, response, status, code=None):
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(set(body), {'code', 'message', 'request_id'})
        self.assertEqual(response.headers['x-request-id'], body['request_id'])
        if code:
            self.assertEqual(body['code'], code)
        for secret in ('PRIVATE', TOKEN, 'postgresql', 'Traceback'):
            self.assertNotIn(secret, response.text)

    def test_submission_to_result_and_events_with_real_storage(self):
        with self.client() as client:
            self.assertEqual(client.get('/health/live').json(), {'status': 'live'})
            self.assertEqual(client.get('/health/ready').status_code, 200)
            response = self.submit(client)
            self.assertEqual(response.status_code, 202, response.text)
            info = response.json()
            self.assertEqual(info['model_mode'], 'mock')
            task = self.wait(client, info['task_id'])
            self.assertEqual(task['review_status'], 'COMPLETE')
            self.assertEqual(task['config']['model_mode'], 'mock')
            self.assertEqual(task['publication_state'], 'disabled')
            self.assertEqual(task['head_sha'], HEAD)
            self.assertEqual(task['owner'], 'test')
            self.assertNotIn('idempotency_key', task)
            result = client.get(info['result_url'])
            self.assertEqual(result.status_code, 200, result.text)
            self.assertFalse(result.json()['partial'])
            self.assertEqual(result.json()['model_mode'], 'mock')
            self.assertEqual(len(result.json()['files']), 1)
            events, seq = [], 0
            while True:
                page = client.get(info['events_url'], params={'after_seq': seq, 'limit': 2})
                self.assertEqual(page.status_code, 200, page.text)
                data = page.json()
                events.extend(data['items'])
                seq = data['next_after_seq']
                if not data['has_more']:
                    break
            self.assertEqual([item['seq'] for item in events], list(range(1, len(events) + 1)))
            self.assertIsNone(next(e for e in events if e['node'] == 'model_request')['api_usage'])
            self.assertEqual(self.provider.prepare.call_args.args[0]['head_sha'], HEAD)
        self.publish.assert_not_called()
        self.assertTrue(client.app.state.shutdown_complete)

    def test_empty_and_file_failure_coverage(self):
        for files, status in (([], 'NO_ELIGIBLE_FILES'), ([FileDiff('app.py', '+x')], 'INCOMPLETE')):
            with self.subTest(status=status):
                self.provider.prepare.side_effect = lambda task, check: fixture_provider(task, check, files=files)
                model = Mock(side_effect=TimeoutError('PRIVATE response'))
                with self.client(model=model) as client:
                    task_id = self.submit(client).json()['task_id']
                    task = self.wait(client, task_id)
                    self.assertEqual(task['execution_status'], 'finished')
                    self.assertEqual(task['review_status'], status)
                    result = client.get(f'/api/v1/reviews/{task_id}/result').json()
                    self.assertFalse(result['partial'])
                    self.assertEqual(model.call_count, len(files))
                    self.assertNotIn('PRIVATE', str(result))

    def test_authentication_precedes_parsing_and_every_business_route_is_protected(self):
        with self.client() as client:
            client.headers.clear()
            for path in ('/api/v1/reviews', f'/api/v1/reviews/{uuid4()}',
                         f'/api/v1/reviews/{uuid4()}/result', f'/api/v1/reviews/{uuid4()}/events'):
                self.assert_error(client.get(path), 401)
            self.assert_error(client.post('/api/v1/reviews', content='PRIVATE invalid json'), 401)
            self.assert_error(client.get('/api/v1/reviews', headers={'Authorization': 'Bearer wrong'}), 401)
            self.assertEqual(client.get('/health/live').status_code, 200)
        self.provider.metadata.assert_not_called()

    def test_invalid_input_unknown_fields_keys_and_repository_rejected_before_metadata(self):
        with self.client() as client:
            for changes in ({'pr_number': True}, {'pr_number': 0}, {'owner': '../bad'}, {'repo': '..'},
                            {'expected_head_sha': 'bad'}, {'model_url': 'PRIVATE'}, {'github_token': 'PRIVATE'},
                            {'publish': True}, {'local_path': 'PRIVATE'}, {'profile': 'missing'}):
                self.assert_error(self.submit(client, body={**BODY, **changes}), 422)
            self.assert_error(client.post('/api/v1/reviews', json=BODY), 422)
            self.assert_error(self.submit(client, key='invalid'), 422)
            self.assert_error(self.submit(client, body={**BODY, 'repo': 'other'}), 403)
            self.assert_error(client.post('/api/v1/reviews', content='{PRIVATE',
                headers={'Content-Type': 'application/json', 'Idempotency-Key': str(uuid4())}), 422)
        self.provider.metadata.assert_not_called()

    def test_body_limit_applies_to_length_and_streamed_body(self):
        with self.client() as client:
            for content in ('x' * 16385, iter([b'x' * 8192, b'x' * 8193])):
                self.assert_error(client.post('/api/v1/reviews', content=content,
                    headers={'Idempotency-Key': str(uuid4()), 'Content-Type': 'application/json'}), 413)
        self.provider.metadata.assert_not_called()

    def test_replay_precedes_metadata_and_profile_configuration_changes(self):
        key = str(uuid4())
        with self.client() as client:
            first = self.submit(client, key=key).json()
            self.wait(client, first['task_id'])
            self.provider.metadata.side_effect = AssertionError('must not fetch changed PR')
            client.app.state.settings = self.settings.model_copy(update={'profiles': {'small': StoredConfig(max_files=1)}})
            replay = self.submit(client, key=key, body={**BODY, 'owner': 'test', 'repo': 'repo'})
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()['task_id'], first['task_id'])
            self.assertEqual(replay.json()['head_sha'], HEAD)
            self.assert_error(self.submit(client, key=key, body={**BODY, 'pr_number': 2}), 409)
        self.assertEqual(self.provider.metadata.call_count, 1)

    def test_upstream_errors_and_revision_conflict_create_no_task(self):
        with self.client() as client:
            for failure, status in ((GitHubAPIError('PRIVATE'), 502), (UpstreamTimeout('PRIVATE'), 504),
                                    (ReviewSupersededError('PRIVATE'), 409)):
                self.provider.metadata.side_effect = failure
                self.assert_error(self.submit(client), status)
            self.provider.metadata.side_effect = None
            self.assert_error(self.submit(client, body={**BODY, 'expected_head_sha': 'c' * 40}), 409)
            self.assertEqual(client.get('/api/v1/reviews').json()['items'], [])

    def test_missing_tasks_and_invalid_query_cursors(self):
        with self.client() as client:
            for suffix in ('', '/result', '/events'):
                self.assert_error(client.get(f'/api/v1/reviews/{uuid4()}{suffix}'), 404)
            for query in ('limit=0', 'limit=101', 'cursor=garbage', 'unexpected=1'):
                self.assert_error(client.get('/api/v1/reviews?' + query), 422)
            for query in ('limit=501', 'after_seq=-1', 'after_seq=9223372036854775808', 'unexpected=1'):
                self.assert_error(client.get(f'/api/v1/reviews/{uuid4()}/events?' + query), 422)

    def test_concurrent_same_key_returns_one_task(self):
        key = str(uuid4())
        with self.client() as client:
            with ThreadPoolExecutor(max_workers=6) as pool:
                responses = list(pool.map(lambda _: self.submit(client, key=key), range(6)))
            self.assertEqual(sorted(r.status_code for r in responses), [200] * 5 + [202])
            identities = {r.json()['task_id'] for r in responses}
            self.assertEqual(len(identities), 1)
            self.wait(client, identities.pop())
            self.assertEqual(len(client.get('/api/v1/reviews').json()['items']), 1)
        self.assertEqual(self.provider.prepare.call_count, 1)

    def test_task_pagination_is_stable_across_insert(self):
        with self.client() as client:
            identities = []
            for _ in range(3):
                task_id = self.submit(client).json()['task_id']
                identities.append(task_id)
                self.wait(client, task_id)
            first = client.get('/api/v1/reviews?limit=1').json()
            extra = self.submit(client).json()['task_id']
            self.wait(client, extra)
            cursor, seen = first['next_cursor'], [first['items'][0]['id']]
            while cursor:
                page = client.get('/api/v1/reviews', params={'limit': 1, 'cursor': cursor}).json()
                seen.extend(item['id'] for item in page['items'])
                cursor = page['next_cursor']
            self.assertEqual(seen, list(reversed(identities)))

    def test_queue_capacity_replay_and_partial_results(self):
        entered, release = threading.Event(), threading.Event()
        def model(*args, **kwargs):
            entered.set()
            release.wait(5)
            return mock_context_request(*args, **kwargs)
        with self.client(settings=self.settings.model_copy(update={'waiting_limit': 1}), model=model) as client:
            try:
                running = self.submit(client).json()['task_id']
                self.assertTrue(entered.wait(4))
                key = str(uuid4())
                queued = self.submit(client, key=key).json()['task_id']
                self.assert_error(self.submit(client), 429)
                self.assertEqual(self.submit(client, key=key).status_code, 200)
                for task_id in (running, queued):
                    result = client.get(f'/api/v1/reviews/{task_id}/result').json()
                    self.assertTrue(result['partial'])
                    self.assertEqual(result['files'], [])
            finally:
                release.set()

    def test_second_app_fails_readiness_and_cannot_submit_but_live_responds(self):
        with self.client() as first, self.client() as second:
            self.assertEqual(first.get('/health/ready').status_code, 200)
            self.assertEqual(second.get('/health/live').status_code, 200)
            self.assert_error(second.get('/health/ready'), 503)
            self.assert_error(self.submit(second), 503)
        self.provider.metadata.assert_not_called()

    def test_database_failure_clears_readiness_and_errors_are_safe(self):
        with self.client() as client:
            with patch.object(client.app.state.repository, 'list_tasks',
                              side_effect=OperationalError('PRIVATE SQL', {}, RuntimeError('PRIVATE PASSWORD'))):
                self.assert_error(client.get('/api/v1/reviews'), 503)
            self.assert_error(client.get('/health/ready'), 503)
            self.assert_error(self.submit(client), 503)
            self.assertEqual(client.get('/health/live').status_code, 200)

    def test_restart_keeps_results_and_replay_without_upstream(self):
        key = str(uuid4())
        with self.client() as client:
            task_id = self.submit(client, key=key).json()['task_id']
            self.wait(client, task_id)
        self.provider.metadata.side_effect = AssertionError('must not refetch')
        with self.client() as client:
            self.assertEqual(self.submit(client, key=key).json()['task_id'], task_id)
            self.assertEqual(len(client.get(f'/api/v1/reviews/{task_id}/result').json()['files']), 1)

    def test_profile_budget_is_enforced_and_snapshot_is_visible(self):
        settings = self.settings.model_copy(update={'profiles': {'default': StoredConfig(max_model_calls=0)}})
        model = Mock(side_effect=AssertionError('budget must reject before model'))
        with self.client(settings=settings, model=model) as client:
            task_id = self.submit(client).json()['task_id']
            task = self.wait(client, task_id)
            self.assertEqual(task['review_status'], 'INCOMPLETE')
            self.assertEqual(task['config']['max_model_calls'], 0)
        model.assert_not_called()

    def test_unexpected_error_is_redacted(self):
        self.provider.metadata.side_effect = RuntimeError('PRIVATE SOURCE AND TOKEN')
        with self.client() as client:
            self.assert_error(self.submit(client), 500)

    def test_blocked_metadata_does_not_block_liveness(self):
        entered, release = threading.Event(), threading.Event()
        def metadata(body):
            entered.set()
            release.wait(5)
            return PullRequestRevision(HEAD, BASE)
        self.provider.metadata.side_effect = metadata
        with self.client() as client, ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(self.submit, client)
            try:
                self.assertTrue(entered.wait(3))
                response = pool.submit(client.get, '/health/live').result(timeout=2)
                self.assertEqual(response.status_code, 200)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5).status_code, 202)

    def test_real_connection_failure_and_schema_mismatch_are_not_ready(self):
        # Connect to a closed loopback port; never stop the shared test DB server.
        unavailable = create_database_engine(self.engine.url.set(port=1))
        try:
            app = create_app(self.settings, engine=unavailable, provider=self.provider)
            with TestClient(app, headers=AUTH) as client:
                self.assertEqual(client.get('/health/live').status_code, 200)
                self.assert_error(client.get('/health/ready'), 503)
                self.assert_error(self.submit(client), 503)
        finally:
            unavailable.dispose()
        with patch('review_api.executor.schema_ready', return_value=False):
            with self.client() as client:
                self.assert_error(client.get('/health/ready'), 503)
                self.assert_error(self.submit(client), 503)
        self.provider.metadata.assert_not_called()

    def test_restart_serves_partial_findings_with_original_evidence_and_unknown_resources(self):
        repository = ReviewRepository(session_factory(self.engine))
        task, _ = repository.submit(uuid4(), ReviewSubmission(owner='test', repo='repo', pr_number=1), snapshot())
        repository.claim_next()
        repository.save_file(task['id'], checkpoint(findings=(finding(),)))
        with self.client() as client:
            response = client.get(f"/api/v1/reviews/{task['id']}/result")
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertTrue(result['partial'])
            self.assertEqual(result['execution_status'], 'interrupted')
            self.assertFalse(result['resource_records_complete'])
            self.assertEqual(result['files'][0]['findings'][0]['evidence'][0]['revision'], HEAD)
        self.provider.prepare.assert_not_called()

    def test_shutdown_grace_expiry_signals_process_exit_without_disposing_active_worker(self):
        entered, release = threading.Event(), threading.Event()
        def model(*args, **kwargs):
            entered.set()
            release.wait(5)
            return mock_context_request(*args, **kwargs)
        client = self.client(settings=self.settings.model_copy(update={'shutdown_grace': 0}), model=model)
        try:
            with self.assertRaisesRegex(RuntimeError, 'process exit required'):
                with client:
                    self.assertEqual(self.submit(client).status_code, 202)
                    self.assertTrue(entered.wait(3))
            self.assertFalse(client.app.state.shutdown_complete)
            self.assertFalse(client.app.state.executor.ready)
        finally:
            release.set()
            if hasattr(client.app.state, 'executor'):
                self.assertTrue(client.app.state.executor.stop(grace=5))
