"""Offline checks for test credentials; never start Docker or connect to a DB."""
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest

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


if __name__ == '__main__':
    unittest.main()
