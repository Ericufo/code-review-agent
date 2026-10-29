"""Create a new disposable test database; never migrate or clear a supplied DB.

The supplied URL must point to a local dedicated review_test_admin database.
Only the newly created review_test_<uuid> database is migrated and destroyed.
"""
import argparse
import os
from pathlib import Path
import re
import sys
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from review_api.db import create_database_engine, database_url


def validate_test_admin(value):
    url = database_url(value)
    if (url.database != 'review_test_admin' or url.host not in {'127.0.0.1', 'localhost', '::1'}
            or url.query):
        raise ValueError('Tests require a local review_test_admin database URL without query overrides')
    return url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, help='Explicit test-only env file; never auto-load .env')
    args = parser.parse_args()
    values = dotenv_values(args.env_file, encoding='utf-8-sig') if args.env_file else os.environ
    try:
        admin_url = validate_test_admin(values.get('REVIEW_TEST_DATABASE_URL', ''))
    except ValueError as exc:
        parser.exit(2, f'{exc}\n')
    name = 'review_test_' + uuid4().hex
    assert re.fullmatch(r'review_test_[0-9a-f]{32}', name)
    engine = create_database_engine(admin_url).execution_options(isolation_level='AUTOCOMMIT')
    created = False
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
        created = True
        os.environ['REVIEW_TEST_DATABASE_URL'] = admin_url.set(database=name).render_as_string(hide_password=False)
        os.environ['REVIEW_ISOLATED_TEST_DB'] = name
        print(f'Created isolated PostgreSQL test database: {name}', flush=True)
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests' / 'postgres'))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    finally:
        if created:
            # The name is generated above, never taken from a caller's URL.
            with engine.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
            print(f'Removed isolated test database: {name}', flush=True)
        engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
