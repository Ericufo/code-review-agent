"""Offline provider/configuration tests alongside optional API dependencies."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import requests
from pydantic import ValidationError

from github_client import GitHubAPIError
from github_review import ReviewSupersededError
from repository_context import ContextError
from review_api.providers import GitHubProvider, ReadOnlyGitHub, LiveModelTransport, UpstreamTimeout
from review_api.settings import Settings
from review_api.storage_types import ReviewSubmission, StoredConfig
from test_storage import HEAD, BASE


def settings(**values):
    return Settings(**{'database_url': 'postgresql+psycopg://user:PRIVATE@localhost/test',
        'api_token': 'PRIVATE_TOKEN', 'allowed_repos': {'Test/Repo'}, **values})


def response(data, *, status=200):
    result = Mock()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.status_code = status
    result.iter_content.return_value = [json.dumps(data).encode()]
    return result


class TestAPIConfiguration(unittest.TestCase):
    def test_omitted_profiles_follow_model_mode_and_match_environment(self):
        for mode in ('mock', 'live'):
            with self.subTest(mode=mode):
                value = settings(model_mode=mode, model_key='PRIVATE_MODEL')
                self.assertEqual(value.profiles['default'].model_mode, mode)
                env = {'DATABASE_URL': 'postgresql+psycopg://user:PRIVATE@localhost/test',
                       'REVIEW_API_TOKEN': 'PRIVATE_TOKEN', 'REVIEW_ALLOWED_REPOS': 'Test/Repo',
                       'REVIEW_MODEL_MODE': mode, 'DEEPSEEK_API_KEY': 'PRIVATE_MODEL'}
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(value.profiles, Settings.from_env().profiles)
        first = settings()
        first.profiles.clear()
        self.assertIn('default', settings().profiles)

    def test_explicit_profiles_are_preserved_and_mismatches_rejected(self):
        profiles = {'small': StoredConfig(model_mode='live', max_model_calls=2)}
        value = settings(model_mode='live', model_key='PRIVATE_MODEL', profiles=profiles)
        self.assertEqual(value.profiles, profiles)
        for explicit in ({}, {'default': StoredConfig()}, None):
            with self.subTest(profiles=explicit), self.assertRaises(ValidationError):
                settings(model_mode='live', model_key='PRIVATE_MODEL', profiles=explicit)
        with self.assertRaisesRegex(ValidationError, 'Live mode requires a server model key'):
            settings(model_mode='live')

    def test_container_database_url_quotes_password_and_requires_nonempty_secret(self):
        from review_api.container import configure_database
        from review_api.db import database_url
        with patch.dict(os.environ, {'POSTGRES_PASSWORD': 'test@:/?#%password',
                                     'POSTGRES_HOST': 'db', 'POSTGRES_USER': 'review', 'POSTGRES_DB': 'review'}, clear=True):
            configure_database()
            value = database_url(os.environ['DATABASE_URL'])
            self.assertEqual(value.password, 'test@:/?#%password')
            self.assertEqual(value.host, 'db')
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            configure_database()

    def test_container_healthcheck_reports_unready_without_exposing_response(self):
        from review_api.healthcheck import main
        with patch('review_api.healthcheck.urlopen', side_effect=OSError('PRIVATE config')):
            self.assertEqual(main(), 1)
        with patch('review_api.healthcheck.urlopen') as get:
            get.return_value.__enter__.return_value.status = 200
            self.assertEqual(main(), 0)
            self.assertEqual(get.call_args.kwargs['timeout'], 3)

    def test_configuration_fails_closed_and_profiles_cannot_increase_limits(self):
        for values in ({'api_token': ''}, {'api_token': 'bad token'}, {'allowed_repos': set()},
                       {'allowed_repos': {'https://bad/repo'}}, {'model_mode': 'other'},
                       {'model_mode': 'live', 'profiles': {'default': StoredConfig(model_mode='live')}},
                       {'profiles': {'default': StoredConfig(max_model_calls=65)}},
                       {'profiles': {'default': StoredConfig(model_mode='live')}},
                       {'github_timeout': 100}, {'shutdown_grace': -1}, {'waiting_limit': 9}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                settings(**values)
        self.assertEqual(settings().allowed_repos, {'test/repo'})
        self.assertNotIn('PRIVATE', repr(settings()))

    def test_environment_errors_are_redacted_and_defaults_are_mock(self):
        env = {'DATABASE_URL': 'postgresql+psycopg://user:PRIVATE@localhost/test',
               'REVIEW_API_TOKEN': 'PRIVATE_TOKEN', 'REVIEW_ALLOWED_REPOS': 'Test/Repo'}
        with patch.dict(os.environ, env, clear=True):
            value = Settings.from_env()
            self.assertEqual(value.model_mode, 'mock')
            self.assertEqual(value.profiles['default'].max_model_calls, 64)
            with patch.dict(os.environ, {'REVIEW_PROFILES_JSON': ''}):
                self.assertEqual(Settings.from_env().profiles, value.profiles)
        for change in ({'DATABASE_URL': 'PRIVATE_INVALID'}, {'REVIEW_API_TOKEN': 'PRIVATE INVALID'},
                       {'REVIEW_PROFILES_JSON': '{PRIVATE'}, {'REVIEW_MODEL_MODE': 'live'}):
            with patch.dict(os.environ, {**env, **change}, clear=True), self.assertRaises(ValueError) as error:
                Settings.from_env()
            self.assertNotIn('PRIVATE', str(error.exception))

    def test_import_does_not_load_env_connect_or_start_threads(self):
        script = '''
from unittest.mock import patch
import dotenv, psycopg, threading, requests, openai, sqlalchemy
def fail(*a, **k): raise AssertionError('side effect on import')
with patch.object(dotenv, 'load_dotenv', fail), patch.object(psycopg, 'connect', fail), \\
     patch.object(threading.Thread, 'start', fail), patch.object(requests, 'get', fail), \\
     patch.object(openai, 'OpenAI', fail), patch.object(sqlalchemy, 'create_engine', fail):
    import review_api.app
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[2], timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_live_transport_uses_explicit_key_timeout_output_cap_and_no_retries(self):
        value = settings(model_mode='live', model_key='PRIVATE_MODEL',
                         profiles={'default': StoredConfig(model_mode='live')}, model_timeout=12, max_output_tokens=100)
        with patch('review_api.providers.OpenAI') as factory:
            transport = LiveModelTransport(value)
            factory.assert_not_called()
            transport([], 'test', tools_enabled=False)
            self.assertEqual(factory.call_args.kwargs['max_retries'], 0)
            self.assertEqual(factory.call_args.kwargs['timeout'], 12)
            self.assertEqual(factory.call_args.kwargs['api_key'], 'PRIVATE_MODEL')
            call = factory.return_value.__enter__.return_value.responses.create.call_args.kwargs
            self.assertEqual(call['max_output_tokens'], 100)
            self.assertEqual(call['tools'], [])
        with self.assertRaises(ValueError):
            LiveModelTransport(settings())


class TestReadOnlyProvider(unittest.TestCase):
    def setUp(self):
        self.provider = GitHubProvider(settings(github_token='PRIVATE_GITHUB'))
        self.submission = ReviewSubmission(owner='test', repo='repo', pr_number=1)
        self.task = {'owner': 'test', 'repo': 'repo', 'pr_number': 1,
                     'head_sha': HEAD, 'base_sha': BASE, 'config': StoredConfig().model_dump()}

    def test_metadata_then_immutable_compare_trees_and_verified_blob(self):
        content = b'x = 1\n'
        oid = hashlib.sha1(f'blob {len(content)}\0'.encode() + content).hexdigest()
        entry = {'path': 'app.py', 'type': 'blob', 'mode': '100644', 'size': len(content), 'sha': oid}
        data = [
            {'state': 'open', 'draft': False, 'head': {'sha': HEAD}, 'base': {'sha': BASE}},
            {'files': [{'filename': 'app.py', 'patch': '@@ -0,0 +1 @@\n+x = 1'}]},
            {'truncated': False, 'tree': [entry]}, {'truncated': False, 'tree': []},
            {'encoding': 'base64', 'size': len(content), 'content': base64.b64encode(content).decode()},
        ]
        guard = Mock()
        with patch('review_api.providers.requests.get', side_effect=[response(x) for x in data]) as get:
            revision = self.provider.metadata(self.submission)
            self.assertEqual(revision.head_sha, HEAD)
            work = self.provider.prepare(self.task, guard)
            self.assertEqual(work.revision, revision)
            self.assertEqual(work.context.read('app.py', 'head'), content.decode())
            self.assertEqual(work.context.read('app.py', 'head'), content.decode())  # cached
        urls = [call.args[0] for call in get.call_args_list]
        self.assertIn(f'compare/{BASE}...{HEAD}?per_page=1&page=1', urls[1])
        self.assertIn('/trees/' + HEAD, urls[2])
        self.assertIn('/trees/' + BASE, urls[3])
        self.assertTrue(urls[4].endswith('/blobs/' + oid))
        self.assertFalse(any('/pulls/' in url for url in urls[1:]))
        self.assertEqual(guard.call_count, 4)
        for call in get.call_args_list:
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertEqual(call.kwargs['timeout'], 10)
        self.assertFalse(hasattr(ReadOnlyGitHub('test', 'repo', settings()), 'create_issue_comment'))

    def test_closed_and_draft_policy_and_invalid_metadata(self):
        for state, draft in (('closed', False), ('open', True)):
            with patch('review_api.providers.requests.get', return_value=response({'state': state, 'draft': draft})):
                with self.assertRaises(ReviewSupersededError):
                    self.provider.metadata(self.submission)
        with patch('review_api.providers.requests.get', return_value=response({'state': 'open'})):
            with self.assertRaises(GitHubAPIError):
                self.provider.metadata(self.submission)

    def test_network_timeout_redirect_bad_json_and_response_size_are_bounded(self):
        with patch('review_api.providers.requests.get', side_effect=requests.Timeout('PRIVATE')):
            with self.assertRaises(UpstreamTimeout) as error:
                self.provider.metadata(self.submission)
            self.assertNotIn('PRIVATE', str(error.exception))
        cases = [response({}, status=302), response({}, status=403), response([])]
        oversized = response({})
        oversized.iter_content.return_value = [b'x' * 8_000_001]
        cases.append(oversized)
        for reply in cases:
            with patch('review_api.providers.requests.get', return_value=reply):
                with self.assertRaises(GitHubAPIError):
                    self.provider.metadata(self.submission)

    def test_ambiguous_comparison_limit_invalid_paths_and_truncated_tree_fail_closed(self):
        for files in (None, [{'filename': f'f{i}.py'} for i in range(300)],
                      [{'filename': '../private.py'}], [{'filename': 'a.py'}, {'filename': 'a.py'}]):
            with patch('review_api.providers.requests.get', return_value=response({'files': files})) as get:
                with self.assertRaises((GitHubAPIError, ValueError)):
                    self.provider.prepare(self.task, Mock())
                self.assertEqual(get.call_count, 1)
        with patch('review_api.providers.requests.get', side_effect=[response({'files': [{'filename': 'a.py'}]}),
                                                                    response({'truncated': True, 'tree': []})]):
            with self.assertRaises(ContextError):
                self.provider.prepare(self.task, Mock())

    def test_guard_prevents_request_and_foreign_url_is_rejected(self):
        guard = Mock(side_effect=RuntimeError('stop'))
        client = ReadOnlyGitHub('test', 'repo', settings(), guard)
        with patch('review_api.providers.requests.get') as get:
            with self.assertRaises(RuntimeError):
                client.get_pull_request(1)
            with self.assertRaises(GitHubAPIError):
                client._get('https://elsewhere.example/PRIVATE')
            get.assert_not_called()
