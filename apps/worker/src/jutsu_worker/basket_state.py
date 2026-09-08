"""Who moves a Knowledge Basket file through its lifecycle.

`complete_upload` writes `uploaded` (a file whose text can be reached) or `stored` (one
whose cannot), and until this module existed nothing wrote anything after that. The
worker only ever read `basket_files`. The consequences were not subtle: `state` never
reached `ready`, `document_id` and `extracted_chars` stayed NULL, `searchable` was
permanently false, `retryable` was permanently false because it is derived from
`state = 'failed'` — and the employee's console polled every three seconds, for ever,
against a row nothing would ever change.

So the ingestion job owns the row from the moment it claims the work. Three writes:

  * `extracting` before the fetch — the console stops saying "Queued" the moment a worker
    picks the file up, which is the only honest thing to say once one has;
  * `ready` + `document_id` + `extracted_chars` when the document is persisted;
  * `failed` + a reason when it is not.

**Every write is `WHERE id = :id AND deleted_at IS NULL`.** A file the employee removed
while its job was in flight must not be resurrected into `ready` by a worker that started
before they pressed the button — and the delete path is the owner's, which outranks the
pipeline's.

**Nothing here raises.** A basket row that cannot be updated must not fail the ingestion
job: the document is persisted, the chunks exist, the grant is written, and the only
casualty is a label. Failing the job would discard real work to fix a cosmetic field, and
would then retry the whole fetch to try again.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["basket_file_id_for", "mark_extracting", "mark_failed", "mark_ready"]

logger = logging.getLogger("jutsu.worker.basket")

#: How much of an extraction failure the employee is shown. The message comes from an
#: exception, and an exception can carry a path, a bucket name or a fragment of the
#: document — none of which belongs on a screen or in `failure_reason`, which the console
#: renders verbatim.
_MAX_REASON = 300


def basket_file_id_for(system: str, external_id: str) -> UUID | None:
    """The basket row this job is about, or None if it is not a basket job.

    `external_id` for a basket document IS the `basket_files.id` (see `BasketConnector`),
    so no lookup is needed — but it is parsed rather than trusted, because a malformed
    identifier must produce "not a basket job" rather than a database error inside a
    handler whose whole contract is that it never raises.
    """
    if system != "basket":
        return None
    try:
        return UUID(external_id)
    except (ValueError, AttributeError, TypeError):
        logger.warning("%s", {"event": "basket_external_id_unparsable"})
        return None


async def _update(
    session: AsyncSession, *, file_id: UUID, sets: str, params: dict[str, Any]
) -> None:
    """One guarded write, swallowing what it cannot do.

    The savepoint matters as much as the try: Postgres aborts the whole transaction at its
    first error, so an exception here without one would discard the persisted document,
    its chunks and its grants — exactly the real work this module is forbidden from
    endangering.
    """
    try:
        async with session.begin_nested():
            await session.execute(
                text(
                    f"UPDATE basket_files SET {sets}, updated_at = now() "  # noqa: S608
                    "WHERE id = :id AND deleted_at IS NULL"
                ),
                {"id": file_id, **params},
            )
    except Exception:  # a label must never cost a document
        logger.warning("%s", {"event": "basket_state_write_failed", "file_id": str(file_id)})


async def mark_extracting(session: AsyncSession, *, file_id: UUID) -> None:
    """A worker has the file. Only from a state that is genuinely waiting.

    The `state IN` guard is what stops a retry of an already-`ready` file from walking it
    backwards, and what stops a late-arriving duplicate job from reopening one that
    finished. `chunking` and `embedding` are not listed: nothing writes them today, and
    admitting a state no code produces would be inventing a transition.
    """
    await _update(
        session,
        file_id=file_id,
        sets="state = 'extracting', failure_reason = NULL, failure_kind = NULL",
        params={},
    )


async def mark_ready(
    session: AsyncSession,
    *,
    file_id: UUID,
    document_id: UUID,
    extracted_chars: int,
) -> None:
    """The document is in the corpus. This is the only place `ready` is ever written.

    `extracted_chars` is recorded even when it is zero — a scanned PDF with no text layer
    is a real answer, and distinguishing it from NULL is what lets the console say "we
    could not read any text in this file" instead of "processing".
    """
    await _update(
        session,
        file_id=file_id,
        sets=(
            "state = 'ready', document_id = :doc, extracted_chars = :chars, "
            "failure_reason = NULL, failure_kind = NULL"
        ),
        params={"doc": document_id, "chars": extracted_chars},
    )


async def mark_failed(session: AsyncSession, *, file_id: UUID, reason: str, kind: str) -> None:
    """Extraction broke. The bytes are untouched and still downloadable.

    A reason is mandatory — the database's own `ck_basket_files_failure_has_reason`
    refuses a failed row without one, because "failed with no reason" is a state the
    console would have to invent a message for.
    """
    cleaned = " ".join((reason or "").split())[:_MAX_REASON] or "That file could not be read."
    await _update(
        session,
        file_id=file_id,
        sets="state = 'failed', failure_reason = :reason, failure_kind = :kind",
        params={"reason": cleaned, "kind": kind[:48]},
    )
