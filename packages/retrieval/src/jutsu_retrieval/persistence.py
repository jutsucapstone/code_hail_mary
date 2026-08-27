"""Writing vectors into `chunks.embedding` (spec §8, §4.5, §4.7).

**Every statement goes through `org_session`.** Row-level security is what makes a
mis-scoped query return nothing instead of another tenant's chunks, and `org_session` is
the only sanctioned way to open a session with the GUC set (ADR 0003). Nothing here uses
`unscoped_session`.

**Resumability is the selection, not a checkpoint.** Pending work is `WHERE embedding IS
NULL`, so a worker that dies halfway through a corpus resumes by asking the same question
again — no cursor to keep, no state to corrupt, and re-running after a full success does
nothing. That is §4.14's idempotency expressed as a query rather than as bookkeeping.

**`token_count` is overwritten with the provider's figure.** ADR 0006 shipped S5 with an
estimate in that column and said the real number arrives here. It does: the value written
is what the provider reported and therefore what was billed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from jutsu_db.engine import org_session
from sqlalchemy import text

from jutsu_retrieval.embeddings import Embedder, EmbeddingTask

__all__ = [
    "EmbeddingRun",
    "PendingChunk",
    "embed_pending_chunks",
    "pending_chunks",
    "store_embeddings",
]

#: Counts and identifiers only. Chunk text is masked but not public — ADR 0005 is explicit
#: that masked text still contains names, because there is no PERSON detector — so no
#: body, no vector and no provider payload is ever logged (§4.9).
logger = logging.getLogger("jutsu.retrieval.embeddings")


@dataclass(frozen=True, slots=True)
class PendingChunk:
    chunk_id: UUID
    body: str


@dataclass(frozen=True, slots=True)
class EmbeddingRun:
    """What one pass did. The numbers a caller needs to report cost and progress."""

    embedded: int
    tokens: int
    requests: int


async def pending_chunks(org_id: UUID, *, limit: int) -> list[PendingChunk]:
    """Chunks in this organisation that have no vector yet, oldest first.

    Ordered by id rather than left to the planner: a stable order makes a partial run
    reproducible, and makes "the same first hundred" mean something when debugging.
    """
    async with org_session(org_id) as session:
        rows = await session.execute(
            text(
                "SELECT id, text FROM chunks "
                "WHERE embedding IS NULL AND org_id = :org_id "
                "ORDER BY id LIMIT :limit"
            ),
            {"org_id": str(org_id), "limit": limit},
        )
        return [PendingChunk(chunk_id=row.id, body=row.text) for row in rows]


async def store_embeddings(
    org_id: UUID,
    chunk_ids: Sequence[UUID],
    vectors: Sequence[Sequence[float]],
    tokens: Sequence[int],
) -> int:
    """Write vectors and the provider's token counts. Returns rows updated.

    An `UPDATE` keyed on the chunk id, inside the org scope, so it is idempotent by
    construction: running it twice writes the same values to the same rows. RLS also means
    a chunk id belonging to another tenant matches nothing — the write silently affects
    zero rows rather than crossing a boundary, which is the correct failure direction.
    """
    if not chunk_ids:
        return 0

    updated = 0
    async with org_session(org_id) as session:
        for chunk_id, vector, token_count in zip(chunk_ids, vectors, tokens, strict=True):
            result = await session.execute(
                text(
                    "UPDATE chunks SET embedding = CAST(:vector AS vector), "
                    "token_count = :token_count "
                    "WHERE id = :chunk_id AND org_id = :org_id "
                    "RETURNING id"
                ),
                {
                    # pgvector accepts its literal text form; the cast is explicit so the
                    # driver does not have to infer the type from a bare string.
                    "vector": "[" + ",".join(repr(float(value)) for value in vector) + "]",
                    "token_count": token_count,
                    "chunk_id": str(chunk_id),
                    "org_id": str(org_id),
                },
            )
            # RETURNING rather than `rowcount`: SQLAlchemy types `execute` as `Result`,
            # which has no rowcount, and counting what came back also proves the row was
            # actually matched — under RLS a chunk belonging to another tenant matches
            # nothing, and that has to be visible rather than indistinguishable from a
            # successful write.
            updated += 1 if result.first() is not None else 0
    return updated


async def embed_pending_chunks(
    org_id: UUID, embedder: Embedder, *, limit: int = 1000
) -> EmbeddingRun:
    """Embed up to `limit` unembedded chunks for one organisation, and store them.

    One pass. Looping until nothing is pending is the job runner's decision (S8), not
    this function's — a function that loops forever is one that cannot be given a budget.

    If embedding raises, nothing is written for that pass. The chunks stay `NULL` and the
    next run picks them up, which is why the selection is the resume mechanism.
    """
    pending = await pending_chunks(org_id, limit=limit)
    if not pending:
        return EmbeddingRun(embedded=0, tokens=0, requests=0)

    before_tokens = embedder.ledger.spent
    before_requests = embedder.ledger.requests

    embeddings = await embedder.embed([chunk.body for chunk in pending], EmbeddingTask.DOCUMENT)

    updated = await store_embeddings(
        org_id,
        [chunk.chunk_id for chunk in pending],
        [embedding.vector for embedding in embeddings],
        [embedding.token_count for embedding in embeddings],
    )

    run = EmbeddingRun(
        embedded=updated,
        tokens=embedder.ledger.spent - before_tokens,
        requests=embedder.ledger.requests - before_requests,
    )
    # Counts and an opaque org id. No chunk id, no text, no vector.
    logger.info(
        "embedding_pass embedded=%d tokens=%d requests=%d", run.embedded, run.tokens, run.requests
    )
    return run
