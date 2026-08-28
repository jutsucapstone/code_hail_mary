"""One document, from source bytes to stored chunks (§9, §4.14, §4.4).

    fetch -> normalise -> ACL capture -> PII mask -> chunk -> persist

Every stage is the existing implementation — `jutsu_connectors` fetches and parses,
`jutsu_core.pii` masks, `jutsu_core.chunking` splits. Nothing here re-implements any of
them, because a second copy of the offset arithmetic is how the two come to disagree about
where a citation points.

**The document is written in one transaction, or not at all.** Document row, ACL grants
and chunks land together. A partial write is the shape of failure that S7 cannot defend
against: a document with no grants is invisible, but a document whose *grants* committed
and whose supersession did not is a document two versions of which are current, and the
partial unique index is what makes that unrepresentable rather than merely unlikely.

**Content decides what happens, and content means the original body.** `RawDocument`
hashes `body` alone — deliberately excluding title and metadata — so a document whose
subject line was edited but whose text is identical is *unchanged* and costs nothing. That
is §4.14 in one sentence, and it is why a second `make seed` writes no rows.

**Masking runs before anything is stored, and the original is stored anyway.** That is not
a contradiction. `body_original` sits behind the ACL check and is what citation offsets
address; `body_masked` and the chunk text are what the model is ever given. ADR 0005 is
explicit that masked text still contains names, so neither is public and only one is sent
away.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from jutsu_core import AclEntry, MaskResult, RawDocument, acl_hash_of
from jutsu_core.chunking import chunk_document
from jutsu_core.pii import mask
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["IngestOutcome", "PersistedDocument", "persist_document"]

#: Counts and opaque identifiers only. Never a body, a chunk, a masked span, a principal
#: or an address — §4.9, and ADR 0005 is explicit that masked text is not safe to log.
logger = logging.getLogger("jutsu.worker.pipeline")

#: The one constraint deferred while a version is superseded. Named rather than
#: `SET CONSTRAINTS ALL DEFERRED`, so the transaction relaxes exactly one check and every
#: other integrity rule still fires at the statement that breaks it.
_DEFER_SUPERSEDE: Final = "SET CONSTRAINTS fk_documents_superseded_by DEFERRED"


class IngestOutcome(StrEnum):
    """What persisting actually did. The audit trail records this, not just "ok"."""

    #: First time this external id has been seen in this source.
    CREATED = "created"
    #: Content changed. A new current version exists and the old one is superseded.
    UPDATED = "updated"
    #: Byte-identical body. Nothing was written at all.
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class PersistedDocument:
    outcome: IngestOutcome
    document_id: uuid.UUID
    #: The version this one replaced, if any. Present so the caller can log a supersession
    #: without reading the row back.
    superseded_id: uuid.UUID | None
    chunk_count: int
    acl_count: int


async def _current_version(
    session: AsyncSession, *, source_id: uuid.UUID, external_id: str
) -> tuple[uuid.UUID, str] | None:
    """The live document for this source identifier, if there is one.

    No `org_id` predicate, and that is not an omission. The row-level security policy
    scopes this to the session's tenant, so adding one would be a second, weaker copy of
    the same rule — and the version this returns is the one the partial unique index calls
    current, which is defined per tenant by that same policy.
    """
    row = (
        await session.execute(
            text(
                "SELECT id, content_hash FROM documents "
                "WHERE source_id = :source AND external_id = :external "
                "AND superseded_by IS NULL"
            ),
            {"source": str(source_id), "external": external_id},
        )
    ).first()
    return (uuid.UUID(str(row.id)), row.content_hash) if row is not None else None


async def _insert_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    org_id: uuid.UUID,
    source_id: uuid.UUID,
    raw: RawDocument,
    masked_text: str,
    acls: list[AclEntry],
) -> None:
    await session.execute(
        text(
            "INSERT INTO documents (id, org_id, source_id, external_id, uri, title, mime, "
            "author_external_id, content_hash, acl_hash, body_original, body_masked, "
            "created_at, modified_at) "
            "VALUES (:id, :org, :source, :external, :uri, :title, :mime, :author, :content, "
            ":acl, :original, :masked, :created, :modified)"
        ),
        {
            "id": str(document_id),
            "org": str(org_id),
            "source": str(source_id),
            "external": raw.external_id,
            "uri": raw.uri,
            "title": raw.title,
            "mime": raw.mime,
            "author": raw.author_external_id,
            "content": raw.content_hash,
            # Stored so a later sync can tell "the grants changed" from "the text
            # changed" without diffing the ACL table.
            "acl": acl_hash_of(acls),
            "original": raw.body,
            "masked": masked_text,
            "created": raw.created_at,
            "modified": raw.modified_at,
        },
    )


async def _insert_acls(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    org_id: uuid.UUID,
    acls: list[AclEntry],
) -> int:
    """Write the grants exactly as the source stated them.

    Nothing is added, defaulted or widened. A document whose participants could not be
    parsed arrives here with an empty list and is therefore stored with **no grants**,
    which makes it invisible rather than public — the correct direction, and the reason
    this function does not quietly substitute an organisation-wide grant when the list is
    empty.
    """
    for entry in acls:
        await session.execute(
            text(
                "INSERT INTO document_acl (document_id, principal_type, principal_id, "
                "org_id, permission) VALUES (:doc, :ptype, :pid, :org, :perm) "
                "ON CONFLICT (document_id, principal_type, principal_id) DO NOTHING"
            ),
            {
                "doc": str(document_id),
                "ptype": entry.principal_type,
                "pid": entry.principal_id,
                "org": str(org_id),
                "perm": entry.permission,
            },
        )
    return len(acls)


async def _insert_chunks(
    session: AsyncSession, *, document_id: uuid.UUID, org_id: uuid.UUID, masked: MaskResult
) -> int:
    """Chunk the already-masked text and store it. Returns how many chunks were written.

    The mask is computed **once** by the caller and passed in, not recomputed here. Two
    calls would agree today — the namespace is the document id and masking is
    deterministic — but they would be two places to change, and the failure if they ever
    diverged is a stored body whose offsets address a different string.

    `embedding` is left NULL. That is the resumption point for `embed.document`, and it is
    a query rather than a checkpoint — the same mechanism S6 already relies on.
    """
    chunks = chunk_document(masked)

    for chunk in chunks:
        await session.execute(
            text(
                "INSERT INTO chunks (id, document_id, org_id, ordinal, text, char_start, "
                "char_end, token_count) "
                "VALUES (:id, :doc, :org, :ordinal, :body, :start, :end, :tokens)"
            ),
            {
                "id": str(uuid.uuid4()),
                "doc": str(document_id),
                "org": str(org_id),
                "ordinal": chunk.ordinal,
                "body": chunk.text,
                # Offsets index the ORIGINAL body, never `chunk.text`. Storing the masked
                # coordinates here would mis-highlight every citation.
                "start": chunk.char_start,
                "end": chunk.char_end,
                "tokens": chunk.token_count,
            },
        )
    return len(chunks)


async def persist_document(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    source_id: uuid.UUID,
    raw: RawDocument,
) -> PersistedDocument:
    """Store one fetched document, versioning it if the content changed.

    Three outcomes, decided by `content_hash` alone:

      * **unchanged** — the live version has the same body. Nothing is written: no
        document, no grants, no chunks, and therefore no embedding work. This is what
        makes a repeated source run free rather than merely safe.
      * **created** — nothing current exists for this identifier.
      * **updated** — the body differs. The live version is superseded and a new current
        version is inserted, in that order, with the self-referential foreign key deferred
        to COMMIT.

    The ordering matters and was chosen by testing it. Inserting the new version first
    puts two rows with `superseded_by IS NULL` in the table at once and the partial unique
    index correctly refuses. Superseding first references a row that does not exist yet,
    which an immediate foreign key refuses. Deferring that one constraint is what lets
    both statements be honest, and there is never an instant with two current versions.

    Runs inside the caller's transaction and commits nothing, so a failure anywhere leaves
    the previous version exactly as it was.
    """
    existing = await _current_version(session, source_id=source_id, external_id=raw.external_id)

    if existing is not None and existing[1] == raw.content_hash:
        logger.info(
            "document_unchanged org=%s source=%s document=%s",
            org_id,
            source_id,
            existing[0],
        )
        return PersistedDocument(
            outcome=IngestOutcome.UNCHANGED,
            document_id=existing[0],
            superseded_id=None,
            chunk_count=0,
            acl_count=0,
        )

    document_id = uuid.uuid4()
    superseded_id: uuid.UUID | None = None

    if existing is not None:
        superseded_id = existing[0]
        await session.execute(text(_DEFER_SUPERSEDE))
        await session.execute(
            text("UPDATE documents SET superseded_by = :new WHERE id = :old"),
            {"new": str(document_id), "old": str(superseded_id)},
        )

    # Masked once, namespaced by the document id so two documents never share a
    # pseudonym (ADR 0005). A new version is a new document and gets its own tokens,
    # which is consistent rather than surprising: pseudonyms describe one version's text,
    # and cross-document identity goes through the vault instead.
    masked = mask(raw.body, namespace=str(document_id))

    await _insert_document(
        session,
        document_id=document_id,
        org_id=org_id,
        source_id=source_id,
        raw=raw,
        masked_text=masked.masked_text,
        acls=raw.acls,
    )
    acl_count = await _insert_acls(session, document_id=document_id, org_id=org_id, acls=raw.acls)
    chunk_count = await _insert_chunks(
        session, document_id=document_id, org_id=org_id, masked=masked
    )

    outcome = IngestOutcome.UPDATED if superseded_id else IngestOutcome.CREATED
    # Identifiers and counts. No title, no body, no principal — a principal is a provider
    # subject and therefore personal data.
    logger.info(
        "document_%s org=%s source=%s document=%s chunks=%d grants=%d",
        outcome.value,
        org_id,
        source_id,
        document_id,
        chunk_count,
        acl_count,
    )
    return PersistedDocument(
        outcome=outcome,
        document_id=document_id,
        superseded_id=superseded_id,
        chunk_count=chunk_count,
        acl_count=acl_count,
    )
