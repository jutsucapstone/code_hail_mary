"""Alembic environment.

Async throughout, because the application engine is asyncpg and running migrations
through a second, sync driver would mean two dialect code paths to keep in step.

The URL comes from the environment, never from alembic.ini — §4.10 keeps credentials
out of the repository.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from jutsu_db.models import Base
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """The URL migrations connect through.

    `MIGRATION_DATABASE_URL` wins because the two are different roles: the application
    connects as a NOSUPERUSER NOBYPASSRLS role that cannot run DDL, and only the owner
    can. Falls back to `DATABASE_URL` for deployments where one role does both.
    """
    # An explicitly configured URL — what the test suite passes — outranks the
    # environment, so a stray DATABASE_URL cannot redirect a migration at the wrong
    # database.
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured

    url = os.environ.get("MIGRATION_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "Neither MIGRATION_DATABASE_URL nor DATABASE_URL is set. "
            "Copy .env.example to .env, or run `make up` first."
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL without connecting — used for reviewing a migration as text."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Type and server-default changes must show up in autogenerate diffs, or a
        # migration silently drifts from the models.
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # `vector` is created by the extension, not by SQLAlchemy; without this
        # autogenerate proposes dropping and recreating the column on every run.
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Keep LangGraph's own checkpoint tables out of autogenerate.

    They live in the `langgraph` schema (§8) and are managed by the checkpointer, not by
    us. Without this, the first autogenerate after S13 would propose dropping them.
    """
    schema = getattr(obj, "schema", None)
    return schema != "langgraph"


async def run_async_migrations() -> None:
    config.set_main_option("sqlalchemy.url", _database_url())
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
