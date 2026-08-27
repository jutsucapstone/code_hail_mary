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
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from jutsu_core.errors import NotFound
from jutsu_db.acl import resolve_acl_principals
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_retrieval.search import ACL_PREDICATE, ORG_SCOPE_SQL, Evidence

__all__ = ["fetch_evidence"]

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
