"""Run migrations with an injected connection or an environment URL."""

import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

from techcom_followup.adapters.outbound.orm import metadata


def migrate(connection: Connection) -> None:
    """Apply migrations on a caller-owned transaction/connection.

    Each revision commits on its own (inside a caller's transaction they
    all share it), so a later revision can take weaker locks than the
    exclusive ones an earlier revision held until its commit.
    """
    context.configure(
        connection=connection,
        target_metadata=metadata,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run() -> None:
    """Support injected connections and CLI online/offline modes."""
    connection = context.config.attributes.get("connection")
    if connection is not None:
        migrate(connection)
        return
    url = os.environ["FOLLOWUP_DATABASE_URL"]
    if context.is_offline_mode():
        context.configure(
            url=url,
            target_metadata=metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as owned:
            migrate(owned)
    finally:
        engine.dispose()


run()
