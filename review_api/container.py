"""Container entry point; build the database URL without interpolating raw passwords."""
import os
import sys

from sqlalchemy.engine import URL


def configure_database():
    password = os.environ.get('POSTGRES_PASSWORD', '')
    if not password:
        raise ValueError('POSTGRES_PASSWORD is required')
    os.environ['DATABASE_URL'] = URL.create('postgresql+psycopg',
        username=os.environ.get('POSTGRES_USER', 'review'), password=password,
        host=os.environ.get('POSTGRES_HOST', 'db'), port=5432,
        database=os.environ.get('POSTGRES_DB', 'review')).render_as_string(hide_password=False)


def main():
    role = sys.argv[1] if len(sys.argv) == 2 else ''
    if role not in {'api', 'migrate'}:
        raise SystemExit('Expected api or migrate')
    try:
        configure_database()
        if role == 'migrate':
            from alembic import command
            from review_api.db import migration_config
            command.upgrade(migration_config(), 'head')
            print('Database migration completed', flush=True)
            return
        from review_api.settings import Settings
        Settings.from_env()
    except Exception:
        raise SystemExit('Database migration or server configuration failed; check service configuration') from None
    import uvicorn
    uvicorn.run('review_api.app:create_app', factory=True, host='0.0.0.0', port=8000,
                workers=1, access_log=False, timeout_graceful_shutdown=10)


if __name__ == '__main__':
    main()
