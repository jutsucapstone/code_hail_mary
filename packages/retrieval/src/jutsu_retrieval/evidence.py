"""Fetching one chunk by id, under the same ACL filter as search (§15, §4.5, §4.6).

`GET /v1/evidence/{chunk_id}` is the endpoint a citation marker resolves to: the reader
clicks `[3]` and expects the source span highlighted in the real document. That makes it a
second door onto exactly the evidence `search_chunks` guards, and a second door is where
authorization gets forgotten — the search is obviously security-critical, the "just fetch
one row by id" helper next to it looks like plumbing.

So it reuses `ACL_PREDICATE` verbatim rather than re-deriving the check. One predicate,
one place to review, and no way for the two paths to drift into disagreeing about who may
read what.

**A chunk the caller may not read is reported as absent, not as forbidden.** A 403 would
confirm the chunk exists, which turns this endpoint into an oracle: feed it ids and read
the tenant's document population off the status codes. `NotFound` is the same answer for
"never existed", "another tenant's" and "not granted to you".

**`fetch_evidence_many` is the third door, and it is the one GraphRAG depends on.**
Neo4j has no row-level security and is not an authorization surface (ADR 0007, ADR 0022):
graph retrieval returns chunk *identifiers*, and this function is where those identifiers
become readable evidence or silently do not. It runs the same `ACL_PREDICATE`, resolves
principals the same way, and returns only what came back — a graph candidate the caller
may not read is absent from the result, with no count, no error and nothing reaching the
model. That is the invariant the security tests assert, and the reason this lives beside
`fetch_evidence` rather than in the graph package: one predicate, one place to review.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from uuid import UUID

from jutsu_core.errors import NotFound
from jutsu_db.acl import resolve_acl_principals
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_retrieval.search import ACL_PREDICATE, ORG_SCOPE_SQL, Evidence, vector_literal

__all__ = ["MAX_EVIDENCE_IDS", "fetch_evidence", "fetch_evidence_many"]

#: The most chunk ids one batch fetch will accept.
#:
#: The caller is a retrieval layer fusing a bounded vector page with a bounded graph
#: traversal, so a request above this is a bug rather than a big question — and an
#: unbounded `= ANY(...)` is an unbounded parameter on a request path.
MAX_EVIDENCE_IDS: Final = 200

#: The same shape as the search projection, minus the score — there is no query to be
#: similar to. The ACL predicate is imported, never restated.
_FETCH: Final = (
    "SELECT c.id, c.document_id, c.text, c.char_start, c.char_end, "  # noqa: S608
    "d.title AS document_title, d.created_at AS occurred_at, "
    "CAST(s.system AS text) AS source_system "
    "FROM chunks c "
    "JOIN documents d ON d.id = c.document_id AND d.org_id = c.org_id "
    "JOIN sources s ON s.id = d.source_id "
    f"WHERE c.id = CAST(:chunk_id AS uuid) AND d.org_id = {ORG_SCOPE_SQL} "
    "AND d.superseded_by IS NULL "
    f"AND {ACL_PREDICATE}"
)


async def fetch_evidence(session: AsyncSession, *, user_id: UUID, chunk_id: UUID) -> Evidence:
    """One chunk, if this caller is authorized to read it. Otherwise `NotFound`.

    Principals are resolved here, in the caller's transaction, for the same reason
    `search_chunks` does it: there is no parameter through which a call site could pass a
    wider set, and nothing is cached that a revocation could leave stale.

    `score` is 1.0 — the chunk is exactly itself. It is carried only so that one `Evidence`
    type serves both paths and a citation renderer does not need two.
    """
    principals, groups = await resolve_acl_principals(session, user_id=user_id)

    row = (
        await session.execute(
            text(_FETCH),
            {
                "chunk_id": str(chunk_id),
                "principals": sorted(principals),
                "groups": sorted(groups),
            },
        )
    ).first()

    if row is None:
        raise NotFound("That evidence was not found.")

    return Evidence(
        chunk_id=UUID(str(row.id)),
        document_id=UUID(str(row.document_id)),
        document_title=row.document_title,
        source_system=row.source_system,
        text=row.text,
        char_start=row.char_start,
        char_end=row.char_end,
        score=1.0,
        occurred_at=row.occurred_at,
    )


#: `_FETCH` over a set of ids. Identical in every respect that matters — the same
#: `ACL_PREDICATE`, the same tenant scope from the GUC, the same exclusion of superseded
#: versions — and different only in the one clause that selects rows.
#:
#: The ids are one bound parameter cast to `uuid[]`, never interpolated, so a caller
#: holding a thousand ids cannot build a thousand-branch query out of them.
#:
#: The score expression is the one part that varies, and only between the two constants
#: below. A caller with the query vector gets the real cosine similarity; a caller
#: fetching by id alone gets 1.0, because the chunk is exactly itself and there is nothing
#: for it to be similar to. Assembled by `_fetch_many` rather than by `str.format`, so a
#: brace appearing in the predicate one day cannot turn a security-critical string into a
#: formatting error — the same reason `search.py` assembles its statement in a function.
_FETCH_MANY_TAIL: Final = (
    " AS score, d.title AS document_title, d.created_at AS occurred_at, "
    "CAST(s.system AS text) AS source_system "
    "FROM chunks c "
    "JOIN documents d ON d.id = c.document_id AND d.org_id = c.org_id "
    "JOIN sources s ON s.id = d.source_id "
    "WHERE c.id = ANY(CAST(:chunk_ids AS uuid[])) "
    f"AND d.org_id = {ORG_SCOPE_SQL} "
    "AND d.superseded_by IS NULL "
    f"AND {ACL_PREDICATE}"
)

#: Measured against the same vector the search used. `COALESCE` because a chunk with no
#: embedding yet has no distance, and a NULL score would travel into a ranking as a
#: silently missing value rather than as the "unranked" it actually means.
_SCORE_MEASURED: Final = "COALESCE(1 - (c.embedding <=> CAST(:query AS vector)), 0.0)"

#: No query, no similarity. Stated as a literal rather than left out so both spellings of
#: the statement have the same columns and one row-mapping serves both.
_SCORE_IDENTITY: Final = "1.0"


def _fetch_many(*, measured: bool) -> str:
    """The batch statement, with or without a measured similarity."""
    score = _SCORE_MEASURED if measured else _SCORE_IDENTITY
    return (
        "SELECT c.id, c.document_id, c.text, c.char_start, c.char_end, " + score + _FETCH_MANY_TAIL
    )


async def fetch_evidence_many(
    session: AsyncSession,
    *,
    user_id: UUID,
    chunk_ids: Sequence[UUID],
    query_vector: Sequence[float] | None = None,
) -> tuple[Evidence, ...]:
    """The chunks from `chunk_ids` this caller may read, in the order they were asked for.

    **Absence is the refusal.** Nothing here raises for an unauthorized id, an id from
    another tenant, a superseded version or an id that never existed — all four are simply
    missing from the result. The caller is a retrieval layer assembling context for a
    model, and "you may not see this one" is not a distinction it may make, let alone pass
    on: telling it apart from "no such chunk" is the existence oracle §4.5 forbids.

    Order is preserved because the caller ranked these ids for a reason — a graph
    traversal, a fusion — and re-sorting them by database order would discard the ranking.
    It is presentation only: which rows come back is decided entirely in SQL.

    `query_vector` is how a graph-contributed passage gets an honest `score`. Without it
    every result scores 1.0, which is right for "fetch this chunk by id" and would be a
    lie in a hybrid result set — a passage the graph found would sit in a list of cosine
    similarities claiming a perfect match nobody measured. With it, the same similarity
    the search computes is computed here, over the handful of authorized rows.
    """
    unique: list[str] = []
    seen: set[UUID] = set()
    for chunk_id in chunk_ids:
        if chunk_id not in seen:
            seen.add(chunk_id)
            unique.append(str(chunk_id))
        if len(unique) >= MAX_EVIDENCE_IDS:
            break

    if not unique:
        return ()

    principals, groups = await resolve_acl_principals(session, user_id=user_id)

    parameters: dict[str, object] = {
        "chunk_ids": unique,
        "principals": sorted(principals),
        "groups": sorted(groups),
    }
    statement = _fetch_many(measured=query_vector is not None)
    if query_vector is not None:
        parameters["query"] = vector_literal(query_vector)

    rows = (await session.execute(text(statement), parameters)).all()

    found = {
        UUID(str(row.id)): Evidence(
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
    }

    return tuple(found[UUID(identifier)] for identifier in unique if UUID(identifier) in found)
