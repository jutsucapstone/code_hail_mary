"""Async engine and the org-scoped session that RLS depends on.

The whole tenancy guarantee rests on one thing: every query runs in a transaction where
`app.current_org_id` has been set. `org_session()` is the only sanctioned way to get a
session, so that cannot be forgotten by accident.

Two details carry the whole guarantee.

Transaction scope: connections are pooled, so a plain `SET` outlives the transaction
and rides the connection back into the pool, where the next request — a different org —
inherits it. The scope must be the transaction, not the session.

Parameter binding: `SET LOCAL x = $1` is a syntax error. `SET` is a utility statement
and takes no bind parameters, so the org id would have to be interpolated into SQL —
with a value that arrives from a request context. `set_config(name, value, is_local)`
is the function form: it takes `is_local = true` for transaction scope *and* accepts
bound parameters, so nothing is ever concatenated into the statement.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

#: Postgres GUC read by every RLS policy. Namespaced so it cannot collide with a
#: built-in setting.
ORG_GUC = "app.current_org_id"

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env, or run `make up` first."
        )
    return url


def get_engine() -> AsyncEngine:
    """Process-wide engine, created lazily.

    `pool_pre_ping` costs one round trip per checkout and buys resilience against
    Cloud SQL dropping idle connections, which otherwise surfaces as a random
    `InterfaceError` on the first query of a cold request.
    """
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            database_url(),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            # Set echo via env rather than a literal: SQL text can contain document
            # content, and §4.9 forbids that reaching logs in a deployed environment.
            echo=os.environ.get("SQL_ECHO") == "1",
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)
    return _sessionmaker


async def dispose_engine() -> None:
    """Drop the pool. Call on shutdown, and between tests that swap databases."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def org_session(org_id: UUID) -> AsyncIterator[AsyncSession]:
    """A session bound to one org for the life of one transaction.

    Every RLS policy compares against the GUC this sets, so a query issued outside this
    helper sees nothing at all — `current_setting(..., true)` returns NULL, the
    predicate is NULL, and no row qualifies. Failing closed is deliberate: the failure
    mode of forgetting to scope a session is an empty result, never another org's data.
    """
    session_factory = get_sessionmaker()
    async with session_factory() as session, session.begin():
        # set_config(..., is_local=true) is SET LOCAL in function form, so the org id
        # is a bound parameter rather than string-concatenated into a utility statement.
        await session.execute(
            text("SELECT set_config(:guc, :org_id, true)"),
            {"guc": ORG_GUC, "org_id": str(org_id)},
        )
        yield session


@asynccontextmanager
async def unscoped_session() -> AsyncIterator[AsyncSession]:
    """A session with **no** org scope. Sees nothing on RLS-protected tables.

    Named to be conspicuous in review. Legitimate uses are migrations, health checks and
    queries against tables that carry no tenant data. If this appears in a request path,
    that is a bug: use `org_session` instead.
    """
    session_factory = get_sessionmaker()
    async with session_factory() as session, session.begin():
        yield session


async def ping() -> bool:
    """Cheap liveness probe for `/readyz`."""
    try:
        async with unscoped_session() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
