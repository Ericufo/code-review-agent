"""Alembic entry point: explicit invocation, never request-time create_all."""
import os

from alembic import context

from review_api.db import create_database_engine, database_url
from review_api.models import Base


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata,
                      compare_type=True, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=database_url(os.environ.get('DATABASE_URL', '')),
                      target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    connection = context.config.attributes.get('connection')
    if connection is not None:
        migrate(connection)
    else:
        engine = create_database_engine(os.environ.get('DATABASE_URL', ''))
        try:
            with engine.connect() as connection:
                migrate(connection)
        finally:
            engine.dispose()
