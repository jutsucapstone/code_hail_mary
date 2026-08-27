"""ACL-filtered vector search (§12, §4.5, §4.6, §17).

> A user must never retrieve evidence unless they are authorized to see that evidence, and
> the authorization must happen before the evidence reaches the application.

Everything in this module is that sentence, made structural.

**The filter is a database predicate.** Not a post-filter, not a Python comprehension over
a wider result set, not an instruction to a model. `EXPLAIN` output is asserted to contain
`document_acl`, because the refactor this guards against is the plausible-looking one: a
future maintainer moving the check into Python for readability, at which point the ranking
still looks right and the counts start leaking rows the caller may not read.

**There is no principals parameter.** `search_chunks` takes a `user_id` and resolves the
principal set itself, in the caller's transaction, through `jutsu_db.acl`. A call site
therefore cannot pass a wider set than the database would grant — the class of bug where
authorization is correct everywhere except at one call site is not available here. It also
makes revocation immediate by construction: there is no set to go stale.

**The organisation is not a parameter either.** It is read from `app.current_org_id`, the
same GUC every row-level security policy compares against, so the tenant is whatever the
session was scoped to and no argument can widen it. RLS alone would scope the query; the
explicit predicate is kept because §12 specifies it, because it puts the tenant in the
query plan where a test can see it, and because `org`-type grants are compared against it.

**A short result set is never fixed by loosening the filter.** HNSW is an approximate
index: it returns roughly `ef_search` candidates and *then* the filter runs, so a
restrictive ACL can leave far fewer than `k`. The answer is to search harder — a larger
`ef_search`, pgvector's iterative scan — never to ask for less authorization. The ladder
below re-runs a byte-identical predicate and a test asserts that it does.

**`public` grants are deliberately not honoured.** §12's filter covers `user`, `group` and
`org`; migration 0001's check constraint also permits `public`, which nothing writes. A
grant type nobody has defined the semantics of is treated as no grant, so such a document
is invisible rather than visible to everyone. That is the fail-closed direction, it is
pinned by a test, and giving `public` a meaning is an ADR, not a patch.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from jutsu_db.acl import resolve_acl_principals
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ACL_PREDICATE",
    "DEFAULT_EF_SEARCH_LADDER",
    "DEFAULT_K",
    "DEFAULT_MAX_SCAN_TUPLES",
    "DEFAULT_STATEMENT_TIMEOUT_MS",
    "ORG_SCOPE_SQL",
    "Evidence",
    "SearchPage",
    "SearchStats",
    "search_chunks",
]

#: Counts, identifiers and timings only. Never the question, never chunk text, never a
#: vector — §4.9, and ADR 0005 is explicit that masked text still contains names.
logger = logging.getLogger("jutsu.retrieval.search")

#: §12: "pgvector: top-k=30, ACL-filtered".
DEFAULT_K: Final = 30

#: §12 opens at 100. The rungs above it exist for the restrictive-ACL case and stop at
#: pgvector's own ceiling — `hnsw.ef_search` has `max_val = 1000`, measured on 0.8.6, and
#: a larger value is rejected by the server rather than clamped.
DEFAULT_EF_SEARCH_LADDER: Final = (100, 400, 1000)

#: Bounds pgvector's iterative scan. Without it a filter that matches almost nothing turns
#: every search into a sequential scan of the table wearing an index's clothes.
DEFAULT_MAX_SCAN_TUPLES: Final = 40_000

#: A retrieval that has not answered in this long will not answer usefully. Transaction
#: scoped, so it cannot ride a pooled connection into the next request.
DEFAULT_STATEMENT_TIMEOUT_MS: Final = 5_000

#: The tenant, taken from the GUC rather than from an argument. Public because
#: `evidence.py` shares it: two spellings of the tenant scope is two chances to get
#: one of them wrong.
#:
#: Taken from the GUC rather than from an argument. Identical to the
#: expression in every RLS policy, including the `NULLIF`: `current_setting(…, true)`
#: returns NULL only until the GUC is first set, and an empty string afterwards, which
#: `''::uuid` would *raise* on instead of filtering. Both cases must fail closed.
ORG_SCOPE_SQL: Final = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"

#: §12's filter, with ADR 0010's one change: `= $3` became `= ANY(:principals)`, because a
#: user holds one subject per source system rather than a single portable id.
#:
#: **This string is a security boundary and is never built by interpolation.** It is a
#: module constant so that the escalation loop provably re-runs the same predicate, and so
#: a test can assert that what ran is what is written here.
# S608 flags the interpolation. The only value interpolated is `ORG_SCOPE_SQL`, a
# module constant containing no input of any kind — the tenant it names is read by
# Postgres from a GUC at execution time. Every value that comes from a caller is a
# bound parameter.
ACL_PREDICATE: Final = (
    "EXISTS ("  # noqa: S608
    "SELECT 1 FROM document_acl a "
    "WHERE a.document_id = d.id AND a.permission = 'read' "
    "AND ("
    "(a.principal_type = 'user' AND a.principal_id = ANY(:principals)) "
    "OR (a.principal_type = 'group' AND a.principal_id = ANY(:groups)) "
    f"OR (a.principal_type = 'org' AND a.principal_id = CAST({ORG_SCOPE_SQL} AS text))"
    ")"
    ")"
)


@dataclass(frozen=True, slots=True)
class Evidence:
    """One retrieved chunk, with everything a citation needs (§12 `Citation`).

    `char_start` and `char_end` index the **original** document body, never the masked
    text this object carries. That asymmetry is deliberate and it is the trap CLAUDE.md
    records: the model reads masked text, the user is shown a highlight in the real
    document, and `to_original()` is the only correct translation between them.
    """

    chunk_id: UUID
    document_id: UUID
    document_title: str
    source_system: str
    text: str
    char_start: int
    char_end: int
    #: Cosine similarity in `[0, 1]`, as `1 - distance`. Reported for ranking and for
    #: eval; nothing in this module thresholds on it, because a similarity floor is a
    #: relevance decision and not an authorization one.
    score: float
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class SearchStats:
    """What the search cost. Numbers only, safe to log and to assert on.

    `ef_search` and `attempts` are here because the escalation ladder is otherwise
    invisible: a search that quietly ran three times is a performance fact the caller
    deserves, and a test needs it to prove the ladder stopped.
    """

    attempts: int
    ef_search: int
    returned: int
    elapsed_ms: int
    #: True when escalation stopped with fewer than `k` rows — the ladder ran out, or a
    #: wider search stopped finding anything new. **Not an error and never a reason to
    #: widen the filter**: it usually means the caller is not authorized to see `k`
    #: documents, which is the system working rather than failing.
    exhausted: bool


@dataclass(frozen=True, slots=True)
class SearchPage:
    """Results plus the cursor that continues them.

    The cursor is `(score, chunk_id)` of the last row — a keyset, not an offset. Offset
    pagination re-runs the query and skips rows, so a document becoming visible between
    pages silently shifts the window; a keyset names a position in the ordering instead.
    It carries no authorization: the next page re-resolves principals and re-applies the
    same predicate, so a cursor cannot be edited into access.
    """

    items: tuple[Evidence, ...]
    stats: SearchStats
    next_cursor: tuple[float, UUID] | None


def _vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text form. Bound as a parameter and cast in SQL, never concatenated."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


# The inner scan: `chunks` alone, ordered by the indexed distance expression, with
# authorization as a correlated `EXISTS`. **Both halves of that shape are load-bearing and
# both were measured**, on 40 000 chunks with an organisation-wide grant:
#
#   * `chunks` is the only table in the `FROM`. Joining `documents` and `sources` here
#     makes the planner drive the join from `documents` instead, and the HNSW index is
#     never opened — 3 016 ms.
#   * The `ORDER BY` names the distance **and nothing else**. An index supplies exactly one
#     ordering; adding `, c.id` as a secondary key forces a full sort of every qualifying
#     row and the index is abandoned again — 203 ms even in the chunks-first shape.
#
# With both, the same query is 15 ms and the plan reads `Index Scan using
# ix_chunks_embedding_hnsw`. The tie-break is not lost, it moves outward — see `_ORDER`.
#
# S608: `ORG_SCOPE_SQL` and `ACL_PREDICATE` are constants in this file; `:query`,
# `:principals`, `:groups` and `:k` are bound parameters.
_INNER: Final = (
    "SELECT c.id, c.document_id, c.text, c.char_start, c.char_end, "  # noqa: S608
    "c.embedding <=> CAST(:query AS vector) AS distance "
    "FROM chunks c "
    f"WHERE c.org_id = {ORG_SCOPE_SQL} "
    # An unembedded chunk cannot be ranked. Excluded in SQL so a half-embedded corpus
    # returns fewer rows rather than rows ordered by NULL.
    "AND c.embedding IS NOT NULL "
    "AND EXISTS (SELECT 1 FROM documents d WHERE d.id = c.document_id "
    f"AND d.org_id = {ORG_SCOPE_SQL} "
    # §4.4 — superseding never overwrites, so retrieval has to exclude what was replaced
    # or an answer cites a version that is no longer true.
    "AND d.superseded_by IS NULL "
    f"AND {ACL_PREDICATE})"
)

#: Keyset continuation, applied **inside** the inner scan so the `LIMIT` still lands after
#: authorization. Expressed on `score` rather than distance because that is what the cursor
#: carries, and both sides compute it the same way from the same inputs.
_CURSOR: Final = (
    " AND (1 - (c.embedding <=> CAST(:query AS vector)) < :after_score "
    "OR (1 - (c.embedding <=> CAST(:query AS vector)) = :after_score "
    "AND c.id > CAST(:after_id AS uuid)))"
)

#: The projection, over the `k` rows the inner scan already authorized — and the total
#: order, applied here where it costs a sort of `k` rows instead of the whole corpus.
#:
#: The `h.id` tie-break is not decoration. Cosine ties are common in a corpus full of
#: boilerplate, and without a total order the same query returns the same rows in a
#: different sequence, which makes the keyset cursor lose or repeat rows.
#:
#: What moving it out costs, stated plainly: *which* rows are chosen when several tie at
#: exactly the `k` boundary is left to the index. Nothing above `k` is affected, and HNSW
#: is approximate at that boundary regardless — a tie-break inside the scan would buy
#: determinism there at 13x the latency.
_ORDER: Final = (
    "SELECT h.id, h.document_id, h.text, h.char_start, h.char_end, "  # noqa: S608
    "1 - h.distance AS score, d.title AS document_title, d.created_at AS occurred_at, "
    "CAST(s.system AS text) AS source_system "
    "FROM hits h "
    f"JOIN documents d ON d.id = h.document_id AND d.org_id = {ORG_SCOPE_SQL} "
    "JOIN sources s ON s.id = d.source_id "
    "ORDER BY h.distance, h.id"
)


def _statement(*, paginated: bool) -> str:
    """Assemble the two halves. A CTE so the `LIMIT` binds to the authorized scan."""
    inner = _INNER + (_CURSOR if paginated else "")
    return (
        "WITH hits AS ("
        + inner
        + " ORDER BY c.embedding <=> CAST(:query AS vector) LIMIT :k) "
        + _ORDER
    )


async def _apply_session_limits(session: AsyncSession, *, ef_search: int, timeout_ms: int) -> None:
    """Per-transaction search tuning and resource limits.

    `set_config(…, is_local => true)` rather than `SET LOCAL`, for the reason `org_session`
    documents: `SET` is a utility statement and takes no bind parameters, so a value would
    have to be concatenated into SQL.

    `hnsw.*` is registered by pgvector's library, which is not loaded into a backend until
    something touches a vector. Setting it first is still correct — Postgres accepts it as
    a placeholder and pgvector validates and applies it when the module loads, verified
    against 0.8.6 — and an out-of-range value is rejected loudly rather than ignored.
    """
    await session.execute(
        text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(timeout_ms)}
    )
    await session.execute(
        text("SELECT set_config('hnsw.ef_search', :ef, true)"), {"ef": str(ef_search)}
    )
    # Iterative scan is what makes a restrictive filter recoverable *inside* the index:
    # pgvector keeps scanning until the limit is met instead of stopping at the first
    # `ef_search` candidates. `strict_order` keeps results in exact distance order, which
    # the keyset cursor depends on; `relaxed_order` is faster and would break it.
    await session.execute(text("SELECT set_config('hnsw.iterative_scan', 'strict_order', true)"))
    await session.execute(
        text("SELECT set_config('hnsw.max_scan_tuples', :n, true)"),
        {"n": str(DEFAULT_MAX_SCAN_TUPLES)},
    )


async def search_chunks(
    session: AsyncSession,
    *,
    user_id: UUID,
    query_vector: Sequence[float],
    k: int = DEFAULT_K,
    after: tuple[float, UUID] | None = None,
    ef_search_ladder: Sequence[int] = DEFAULT_EF_SEARCH_LADDER,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> SearchPage:
    """Top-`k` chunks this user is authorized to read, nearest first.

    `session` must already be scoped to the caller's organisation — in the API that is
    `resolve_principal`, in a worker it is `org_session`. An unscoped session reads a NULL
    tenant and matches nothing, which is the correct failure direction and is tested.

    The escalation ladder handles the case §12 warns about. Each rung re-runs
    `ACL_PREDICATE` unchanged with a larger `ef_search`, and stops as soon as one of three
    things is true: `k` rows were found, the ladder is exhausted, or a wider search
    returned no more rows than the previous one. That last condition is what keeps the
    common case cheap — when a caller is authorized to see six documents, no amount of
    searching will find a seventh, and re-running to the top rung every time would pay the
    maximum price for the most restricted user.

    Raises nothing on an empty principal set. A caller with no linked identity matches no
    `user` or `group` grant and simply retrieves nothing (§17 test 1). They may still see
    documents granted to the whole organisation, because an `org` grant is a deliberate
    statement about everyone in the tenant.
    """
    started = time.monotonic()

    # Resolved here, inside the caller's transaction, and never taken as an argument. This
    # is the line that makes a stale or widened principal set unrepresentable.
    principals, groups = await resolve_acl_principals(session, user_id=user_id)

    statement = _statement(paginated=after is not None)
    params: dict[str, object] = {
        "query": _vector_literal(query_vector),
        # asyncpg maps a Python list to a Postgres array, which is what `= ANY(...)`
        # needs. Sorted so the parameter is stable for logging and for plan caching;
        # the set semantics are unaffected.
        "principals": sorted(principals),
        "groups": sorted(groups),
        "k": k,
    }
    if after is not None:
        params["after_score"], params["after_id"] = after[0], str(after[1])

    rows: list[Any] = []
    attempts = 0
    ef_used = 0
    previous_count = -1
    ladder = tuple(ef_search_ladder) or (DEFAULT_EF_SEARCH_LADDER[0],)

    for ef_search in ladder:
        attempts += 1
        ef_used = ef_search
        await _apply_session_limits(session, ef_search=ef_search, timeout_ms=statement_timeout_ms)
        # Same statement, same parameters, larger `ef_search`. The only thing that varies
        # between rungs is how hard the index looks — never what the caller may see.
        rows = list((await session.execute(text(statement), params)).all())

        if len(rows) >= k or len(rows) == previous_count:
            # Enough, or a wider search found nothing new. In the second case the ceiling
            # is the caller's authorization rather than the index, and no amount of extra
            # searching will find a row they are not granted. Stopping there is what keeps
            # the most restricted caller from paying the highest price on every query.
            break
        previous_count = len(rows)

    items = tuple(
        Evidence(
            chunk_id=UUID(str(row.id)),
            document_id=UUID(str(row.document_id)),
            document_title=row.document_title,
            source_system=row.source_system,
            text=row.text,
            char_start=row.char_start,
            char_end=row.char_end,
            score=float(row.score),
            occurred_at=row.occurred_at,
        )
        for row in rows
    )

    stats = SearchStats(
        attempts=attempts,
        ef_search=ef_used,
        returned=len(items),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        exhausted=len(items) < k,
    )
    # Opaque counts and timings. No question, no chunk text, no vector, no principal —
    # a principal is a provider subject and therefore personal data (§4.9).
    logger.info(
        "vector_search returned=%d k=%d attempts=%d ef_search=%d elapsed_ms=%d exhausted=%s",
        stats.returned,
        k,
        stats.attempts,
        stats.ef_search,
        stats.elapsed_ms,
        stats.exhausted,
    )

    next_cursor = (items[-1].score, items[-1].chunk_id) if len(items) == k else None
    return SearchPage(items=items, stats=stats, next_cursor=next_cursor)
