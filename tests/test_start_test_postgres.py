"""Offline checks for test credentials; never start Docker or connect to a DB."""
import os
from contextlib import contextmanager
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts import start_test_postgres as starter
from scripts.start_test_postgres import NAME, write_test_config


class TestTestCredentials(unittest.TestCase):
    def test_credentials_are_private_even_with_permissive_umask(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / '.env.pgtest'
            previous = os.umask(0) if os.name == 'posix' else None
            try:
                write_test_config(config, 'test-only-password', 45678)
            finally:
                if previous is not None:
                    os.umask(previous)
            self.assertEqual(config.read_text(encoding='utf-8'),
                'REVIEW_TEST_DATABASE_URL=postgresql+psycopg://review_test:test-only-password@127.0.0.1:45678/review_test_admin\n'
                f'REVIEW_TEST_CONTAINER={NAME}\n')
            if os.name == 'posix':
                self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

    def test_existing_credentials_are_never_overwritten(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / '.env.pgtest'
            config.write_text('existing configuration', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                write_test_config(config, 'replacement', 45678)
            self.assertEqual(config.read_text(encoding='utf-8'), 'existing configuration')


    def test_failed_write_removes_only_new_partial_config(self):
        @contextmanager
        def broken_open(*args, **kwargs):
            with open(*args, **kwargs):
                yield Mock(write=Mock(side_effect=OSError('disk full')))

        with TemporaryDirectory() as directory:
            config = Path(directory) / '.env.pgtest'
            with patch.object(starter, 'open', broken_open, create=True), self.assertRaises(OSError):
                write_test_config(config, 'test-password', 45678)
            self.assertFalse(config.exists())


class TestStartupRollback(unittest.TestCase):
    container_id = 'a' * 64

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / '.env.pgtest'
        self.port = '127.0.0.1:45678'
        self.names = ''
        self.cleanup_error = False

        def docker(*args, **kwargs):
            if args[0] == 'ps':
                return Mock(stdout=self.names)
            if args[0] == 'run':
                return Mock(stdout=self.container_id + '\n')
            if args[0] == 'port':
                if isinstance(self.port, Exception):
                    raise self.port
                return Mock(stdout=self.port)
            if args[0] == 'rm':
                if self.cleanup_error:
                    raise OSError('Docker unavailable')
                return Mock(stdout=self.container_id)
            raise AssertionError(args)

        for target, value in (('ROOT', self.root), ('docker', Mock(side_effect=docker))):
            patcher = patch.object(starter, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (patch('sys.argv', ['start_test_postgres.py']),
                        patch.object(starter.time, 'sleep'),
                        patch.object(starter.subprocess, 'run', return_value=Mock(returncode=0))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.docker = starter.docker

    def removals(self):
        return [call.args for call in self.docker.call_args_list if call.args[0] == 'rm']

    def test_missing_port_cleans_created_id_and_allows_retry(self):
        self.port = ''
        with self.assertRaisesRegex(SystemExit, 'container was removed'):
            starter.main()
        self.assertEqual(self.removals(), [('rm', '--force', self.container_id)])
        self.assertFalse(self.config.exists())
        self.port = '127.0.0.1:45678'
        self.assertEqual(starter.main(), 0)
        self.assertIn(':45678/review_test_admin', self.config.read_text())
        self.assertEqual(len(self.removals()), 1)

    def test_port_command_failure_timeout_or_invalid_binding_rolls_back(self):
        for value in (OSError('query failed'), subprocess.TimeoutExpired('docker', 10),
                      'invalid', '127.0.0.1:0', '127.0.0.1:65536'):
            with self.subTest(value=value):
                self.port = value
                self.docker.reset_mock()
                with self.assertRaisesRegex(SystemExit, 'container was removed'):
                    starter.main()
                self.assertEqual(self.removals(), [('rm', '--force', self.container_id)])
                self.assertFalse(self.config.exists())

    def test_config_write_failure_rolls_back(self):
        with patch.object(starter, 'write_test_config', side_effect=PermissionError('PRIVATE')):
            with self.assertRaises(SystemExit) as error:
                starter.main()
        self.assertNotIn('PRIVATE', str(error.exception))
        self.assertEqual(self.removals(), [('rm', '--force', self.container_id)])

    def test_racing_config_is_preserved_while_own_container_is_removed(self):
        original = starter.write_test_config

        def raced_write(config, password, port):
            config.write_text('other configuration')
            original(config, password, port)

        with patch.object(starter, 'write_test_config', side_effect=raced_write):
            with self.assertRaises(SystemExit):
                starter.main()
        self.assertEqual(self.config.read_text(), 'other configuration')
        self.assertEqual(self.removals(), [('rm', '--force', self.container_id)])

    def test_cleanup_failure_reports_owned_container(self):
        self.port = ''
        self.cleanup_error = True
        with self.assertRaisesRegex(SystemExit, 'cleanup also failed') as error:
            starter.main()
        self.assertIn(self.container_id, str(error.exception))
        self.assertNotIn('container was removed', str(error.exception))

    def test_existing_container_or_config_is_never_removed(self):
        self.names = NAME
        with self.assertRaises(SystemExit):
            starter.main()
        self.docker.assert_called_once_with('ps', '-a', '--format', '{{.Names}}', timeout=15)
        self.docker.reset_mock()
        self.config.write_text('existing configuration')
        with self.assertRaises(SystemExit):
            starter.main()
        self.docker.assert_not_called()

    def test_readiness_failure_preserves_saved_credentials_and_container(self):
        starter.subprocess.run.return_value.returncode = 1
        with self.assertRaisesRegex(SystemExit, 'Credentials were saved'):
            starter.main()
        self.assertTrue(self.config.exists())
        self.assertEqual(self.removals(), [])
        self.assertEqual(starter.subprocess.run.call_args.args[0][2], self.container_id)


if __name__ == '__main__':
    unittest.main()
