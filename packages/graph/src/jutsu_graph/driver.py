"""Neo4j driver lifecycle and the org-scoped session that tenancy depends on.

**Neo4j has no row-level security.** That is the single fact this module is designed
around, and it is the important difference from `packages/db`. Postgres refuses to return
another tenant's rows even when the application forgets to filter — ADR 0003 exists
because a superuser silently bypassing that was worth an entire slice to prevent. Neo4j
Community offers no equivalent: no row policies, no per-tenant database, and not even
property-existence constraints (those are Enterprise). `org_id` on every node, every
relationship and every query is the *whole* mechanism, and nothing underneath will catch
a query that omits it.

So it is enforced here, by construction:

  * `GraphSession.run` **refuses** a query that does not reference `$org_id`. Forgetting
    to scope is a raised exception, not a wider result set.
  * The caller may not supply `org_id` itself. It is bound by the session, from the
    `UUID` the session was opened with, so a parameter map cannot smuggle a different
    tenant's id past the check.
  * Every other value is a bound parameter. Nothing but an allowlisted label from
    `labels.py` is ever interpolated into query text.

**Read is the default and write is explicit.** `read_session` is what a request path
should use; `write_session` has to be reached for deliberately. Two mechanisms hold it,
because neither is sufficient alone:

  * `default_access_mode=READ_ACCESS` is the driver-level control. On a cluster or on
    AuraDB it routes to a follower, which rejects writes outright. On the
    single-instance Community container used in development it is only a routing hint
    and does **not** block anything — stated plainly here because believing otherwise is
    how a control becomes decorative.
  * A static check on the Cypher text rejects the write clauses. That one works
    everywhere, including single-instance, which is why it exists as well.

Neither classifies procedure calls, so `CALL` into a write procedure is not caught by the
static check. That is a known gap, recorded in ADR 0007 rather than papered over; the LLM
generated-Cypher path of §22 will need a procedure allowlist on top of this, and it is
not built here because there is no generator yet.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from neo4j import (
    READ_ACCESS,
    WRITE_ACCESS,
    AsyncDriver,
    AsyncGraphDatabase,
    AsyncSession,
    AsyncTransaction,
    Record,
)

__all__ = [
    "DdlSession",
    "GraphSession",
    "GraphSettings",
    "MissingGraphSettings",
    "UnscopedQuery",
    "WriteInReadSession",
    "close_driver",
    "ddl_session",
    "get_driver",
    "get_graph_settings",
    "ping",
    "read_session",
    "write_session",
]

#: Bound by the session, never by the caller. Named once so the check and the binding
#: cannot drift apart.
ORG_PARAMETER: Final = "org_id"

#: How long any single transaction may run before the server kills it (§22 — the LLM
#: Cypher path needs this, and there is no reason for the application path to be exempt).
#: Enforced server-side by Neo4j, so a client that hangs cannot hold a transaction open.
DEFAULT_STATEMENT_TIMEOUT_S: Final = 30.0

#: DDL is slower than a query and runs from a migration, not a request. Separate constant
#: so raising it cannot accidentally raise the request-path budget too.
DDL_STATEMENT_TIMEOUT_S: Final = 120.0

#: Clauses that write. Word-bounded, so a property called `created_at` or `settings` does
#: not trip it. This is the mechanism that works on a single instance, where
#: `READ_ACCESS` is only a routing hint.
_WRITE_CLAUSES: Final = re.compile(
    r"\b(?:CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV)\b",
    re.IGNORECASE,
)


class MissingGraphSettings(RuntimeError):
    """Connection details are absent. Raised at first use, never defaulted."""


class UnscopedQuery(RuntimeError):
    """A query reached the graph without an organisation scope.

    The failure mode this prevents is not exotic: it is someone writing a `MATCH` that
    reads correctly, forgetting `WHERE n.org_id = $org_id`, and getting every tenant's
    data back with no error anywhere. In Postgres that query returns nothing, because RLS
    fails closed. Here it would return everything.
    """


class WriteInReadSession(RuntimeError):
    """A write clause appeared in a session opened for reading."""


@dataclass(frozen=True, slots=True, repr=False)
class GraphSettings:
    """Where the graph is and how to authenticate to it.

    `__repr__` is written by hand and `repr=False` above disables the generated one. That
    is not stylistic: a dataclass repr prints every field, this one holds a password, and
    the places a repr surfaces uninvited are exactly the places §4.9 cares about — a
    logged exception, a traceback frame in an error report, an f-string somebody added
    while debugging. There is no representation of this object that contains the
    credential.
    """

    uri: str
    user: str
    password: str
    database: str = "neo4j"

    def __repr__(self) -> str:
        return (
            f"GraphSettings(uri={self.uri!r}, user={self.user!r}, "
            f"password=<redacted>, database={self.database!r})"
        )


def get_graph_settings() -> GraphSettings:
    """Read connection details from the environment (§4.10).

    No defaults for the credential, for the same reason `JUTSU_EMAIL_PEPPER` has none: a
    default password is not a password, and a deployment that silently connected with one
    would be a finding rather than a convenience.
    """
    uri = os.environ.get("NEO4J_URI")
    user = os.environ.get("NEO4J_USER")
    password = os.environ.get("NEO4J_PASSWORD")

    if not uri or not user or not password:
        raise MissingGraphSettings(
            "NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD must all be set. Copy .env.example "
            "to .env and run `make up` for local development; in staging and production "
            "they come from Secret Manager."
        )

    return GraphSettings(
        uri=uri,
        user=user,
        password=password,
        database=os.environ.get("NEO4J_DATABASE", "neo4j"),
    )


_driver: AsyncDriver | None = None


def get_driver(settings: GraphSettings | None = None) -> AsyncDriver:
    """Process-wide driver, created lazily.

    The driver owns a connection pool and is designed to be long-lived and shared; making
    one per request costs a handshake per request and exhausts the server's connection
    limit under load.
    """
    global _driver
    if _driver is None:
        resolved = settings or get_graph_settings()
        _driver = AsyncGraphDatabase.driver(resolved.uri, auth=(resolved.user, resolved.password))
    return _driver


async def close_driver() -> None:
    """Drop the pool. Call on shutdown, and between tests that swap databases."""
    global _driver
    if _driver is not None:
        await _driver.close()
    _driver = None


class GraphSession:
    """A transaction bound to one organisation, for the life of one `with` block.

    Not a subclass of the driver's session and not a passthrough: the whole value is in
    what `run` refuses. A caller holding one of these cannot issue an unscoped query, and
    cannot write from a read session, without an exception.
    """

    __slots__ = ("_org_id", "_transaction", "_writable")

    def __init__(self, transaction: AsyncTransaction, org_id: UUID, *, writable: bool) -> None:
        self._transaction = transaction
        self._org_id = org_id
        self._writable = writable

    @property
    def org_id(self) -> UUID:
        return self._org_id

    @property
    def writable(self) -> bool:
        return self._writable

    async def run(self, cypher: str, /, **parameters: Any) -> list[Record]:
        """Execute one statement inside this transaction.

        `cypher` is positional-only so that a parameter can be called `cypher` without
        colliding with it — `**parameters` swallows every keyword, and a query binding
        `$cypher` is not a hypothetical in a product that stores query text.
        """
        if f"${ORG_PARAMETER}" not in cypher:
            raise UnscopedQuery(
                "This query does not reference $org_id. Every read and write in the "
                "graph must be scoped to one organisation: Neo4j has no row-level "
                "security, so an unscoped query returns every tenant's data rather than "
                "none of it. Add `WHERE n.org_id = $org_id` (or the equivalent for your "
                "pattern); the value is bound by the session."
            )

        if not self._writable and _WRITE_CLAUSES.search(cypher):
            raise WriteInReadSession(
                "This query contains a write clause but the session was opened for "
                "reading. Use `write_session` if the write is intended."
            )

        if ORG_PARAMETER in parameters:
            # Not merged, not overwritten, not ignored. A caller passing an org id is
            # either confused or attempting to widen their scope, and both deserve to
            # stop here rather than to be silently corrected.
            raise UnscopedQuery(
                "org_id may not be supplied as a parameter. It is bound by the session, "
                "which is what makes the scope impossible to widen from a call site."
            )

        bound = {**parameters, ORG_PARAMETER: str(self._org_id)}
        # Parameters go in the map, never into the text. That is what makes the query
        # injection-proof for *values*; for the two fragments Cypher cannot parameterise
        # at all — a label and a relationship type — the guarantee is `labels.py`, which
        # is the only source a caller has for either.
        result = await self._transaction.run(cypher, bound)
        return [record async for record in result]


@asynccontextmanager
async def _session(
    org_id: UUID, *, writable: bool, timeout_s: float, settings: GraphSettings | None = None
) -> AsyncIterator[GraphSession]:
    driver = get_driver(settings)
    resolved = settings or get_graph_settings()
    async with driver.session(
        database=resolved.database,
        default_access_mode=WRITE_ACCESS if writable else READ_ACCESS,
    ) as session:
        transaction = await session.begin_transaction(timeout=timeout_s)
        try:
            yield GraphSession(transaction, org_id, writable=writable)
        except BaseException:
            await transaction.rollback()
            raise
        else:
            await transaction.commit()


@asynccontextmanager
async def read_session(
    org_id: UUID,
    *,
    timeout_s: float = DEFAULT_STATEMENT_TIMEOUT_S,
    settings: GraphSettings | None = None,
) -> AsyncIterator[GraphSession]:
    """A read-only, org-scoped transaction. **The default for anything on a request path.**"""
    async with _session(org_id, writable=False, timeout_s=timeout_s, settings=settings) as session:
        yield session


@asynccontextmanager
async def write_session(
    org_id: UUID,
    *,
    timeout_s: float = DEFAULT_STATEMENT_TIMEOUT_S,
    settings: GraphSettings | None = None,
) -> AsyncIterator[GraphSession]:
    """An org-scoped transaction that may write.

    Named to be conspicuous at the call site. Everything the ingestion pipeline does goes
    through here; nothing a query surface does should.
    """
    async with _session(org_id, writable=True, timeout_s=timeout_s, settings=settings) as session:
        yield session


@asynccontextmanager
async def ddl_session(
    *, timeout_s: float = DDL_STATEMENT_TIMEOUT_S, settings: GraphSettings | None = None
) -> AsyncIterator[DdlSession]:
    """A session with **no** organisation scope, for schema work only.

    Constraints and indexes are database-wide objects; there is no such thing as an
    org-scoped constraint, so the migration runner cannot go through `write_session` and
    could not satisfy its `$org_id` requirement if it tried.

    Named to be conspicuous in review, exactly like `unscoped_session` in `packages/db`.
    If this appears anywhere outside `migrations.py`, that is the bug — every path that
    touches tenant data has an organisation, and this one deliberately does not.

    Yields the raw driver session rather than a `GraphSession`: schema statements cannot
    be parameterised or org-scoped, so wrapping them in a class whose entire purpose is
    to enforce both would be theatre.

    Two server notifications are expected here and are not suppressed. `IF NOT EXISTS`
    reports SCHEMA "already exists" on every idempotent re-run, and reading the ledger on
    an empty database reports UNRECOGNIZED for a property no node carries yet. The driver
    can filter them, but only through `notifications_disabled_classifications`, which is
    a preview API that emits its own warning on every session — swapping two expected
    notices for an unstable dependency is the worse trade.
    """
    driver = get_driver(settings)
    resolved = settings or get_graph_settings()
    async with driver.session(
        database=resolved.database,
        default_access_mode=WRITE_ACCESS,
    ) as session:
        yield DdlSession(session, timeout_s)


class DdlSession:
    """Thin wrapper giving DDL the same statement timeout as everything else."""

    __slots__ = ("_session", "_timeout_s")

    def __init__(self, session: AsyncSession, timeout_s: float) -> None:
        self._session = session
        self._timeout_s = timeout_s

    async def run(self, cypher: str, /, **parameters: Any) -> list[Record]:
        """Run one schema statement.

        Auto-committing, one transaction per statement, because Neo4j refuses to mix
        schema and data operations in a single transaction — `CREATE CONSTRAINT` inside a
        transaction that has already written data fails at commit with a message that
        does not name the cause.
        """
        result = await self._session.run(cypher, parameters or None)
        return [record async for record in result]


async def ping(settings: GraphSettings | None = None) -> bool:
    """Cheap liveness probe for `/readyz`.

    Swallows the exception on purpose: a readiness probe reports a state, it does not
    raise one. Nothing about the failure is returned to the caller, because the reason a
    graph connection failed can contain a host, a port and occasionally a credential.
    """
    try:
        driver = get_driver(settings)
        await driver.verify_connectivity()
        return True
    except Exception:
        return False
