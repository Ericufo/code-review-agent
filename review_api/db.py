"""Explicit database construction; no connections or schema creation on import."""
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]


def database_url(value):
    try:
        url = make_url(value)
    except Exception:
        raise ValueError('Invalid PostgreSQL database URL') from None
    if url.drivername != 'postgresql+psycopg' or not url.database or not url.username or not url.password:
        raise ValueError('A postgresql+psycopg URL with database, user, and nonempty password is required')
    return url


def create_database_engine(url):
    return create_engine(database_url(url), pool_pre_ping=True, hide_parameters=True,
        connect_args={'connect_timeout': 5, 'options': '-c statement_timeout=15000 -c lock_timeout=5000'})


def session_factory(engine):
    # Each repository operation owns its Session; never share across threads.
    return sessionmaker(engine, expire_on_commit=False)


def migration_config(connection=None):
    config = Config(str(ROOT / 'alembic.ini'))
    if connection is not None:
        config.attributes['connection'] = connection
    return config


def schema_ready(engine) -> bool:
    expected = set(ScriptDirectory.from_config(migration_config()).get_heads())
    with engine.connect() as connection:
        actual = set(MigrationContext.configure(connection).get_current_heads())
    return bool(expected) and actual == expected
