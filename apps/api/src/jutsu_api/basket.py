"""The Knowledge Basket: an employee's own files, and what may be done with them.

Bytes go browser → Cloud Storage under a signed URL and never through this process
(ADR 0020). What lives here is the part that decides: which row exists, who may reach it,
and when the bytes are trustworthy enough to hand to the ingestion pipeline.

**Three properties hold across every function below.**

*The row is the authorization.* Every query runs under row-level security, so an
organisation's files are invisible to another's without any predicate being written here.
Within an organisation, `_visible_to` adds the second boundary: you see your own files;
`basket:manage` sees everyone's. That one is an application check because it is a
*product* rule rather than a tenancy one — but it is applied in the SQL, never as a
post-filter, for the reason §4.5 gives about counts and cursors.

*A signed URL is minted only after a row came back.* Not before, not from an id in the
request. The store has no opinion about tenants, so the query IS the check.

*The client's word about its own upload is never trusted.* `complete_upload` re-reads the
object's size and checksum from the store and sniffs its first bytes; the declared type
is kept only as evidence of what was claimed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4

from jutsu_connectors.extraction import plan_for
from jutsu_core.errors import Conflict, NotFound, ServiceUnavailable, ValidationFailed
from jutsu_core.rbac import Permission
from jutsu_core.storage import (
    MAX_UPLOAD_BYTES,
    ObjectStore,
    SignedUpload,
    normalise_filename,
    object_key,
    sanitise_original,
    sniff_mime,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.security import Principal

logger = logging.getLogger("jutsu.basket")

__all__ = [
    "BasketFileRow",
    "UploadTicket",
    "complete_upload",
    "delete_file",
    "download_url",
    "list_files",
    "rename_file",
    "retry_file",
    "start_upload",
]

#: The namespace a basket ACL principal carries, matching `sources.system`.
PRINCIPAL_NAMESPACE: Final = "basket"

#: Columns every read returns, so the API model and the SQL cannot drift.
_COLUMNS: Final = (
    "id, owner_user_id, original_filename, normalised_filename, declared_mime, "
    "detected_mime, size_bytes, state, failure_reason, failure_kind, document_id, "
    "extracted_chars, created_at, updated_at"
)

# S608 appears on every read below, and the exemption is narrow and stated rather than
# applied to the file. Two fragments are interpolated into these queries: `_COLUMNS`, a
# module-level literal, and `scope`, which `_visible_to` returns as one of exactly two
# literals defined in this module. Everything a caller supplies — the id, the owner, the
# search text, the state — is a bound parameter and never reaches the SQL text. Bandit
# cannot tell a constant from user input, which is why the marker is per line.

#: States from which a retry is meaningful. `rejected` and `quarantined` are not here:
#: the first means the bytes were not what they claimed and the second means the file was
#: refused for a security reason, and re-running either would fail identically.
_RETRYABLE: Final = ("failed",)


@dataclass(frozen=True, slots=True)
class UploadTicket:
    file_id: UUID
    upload: SignedUpload


@dataclass(frozen=True, slots=True)
class BasketFileRow:
    id: UUID
    owner_user_id: UUID
    original_filename: str
    normalised_filename: str
    declared_mime: str
    detected_mime: str | None
    size_bytes: int
    state: str
    failure_reason: str | None
    failure_kind: str | None
    document_id: UUID | None
    extracted_chars: int | None
    created_at: datetime
    updated_at: datetime


def _row(record: Any) -> BasketFileRow:
    return BasketFileRow(
        id=UUID(str(record.id)),
        owner_user_id=UUID(str(record.owner_user_id)),
        original_filename=record.original_filename,
        normalised_filename=record.normalised_filename,
        declared_mime=record.declared_mime,
        detected_mime=record.detected_mime,
        size_bytes=int(record.size_bytes),
        state=record.state,
        failure_reason=record.failure_reason,
        failure_kind=record.failure_kind,
        document_id=UUID(str(record.document_id)) if record.document_id else None,
        extracted_chars=record.extracted_chars,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _visible_to(actor: Principal) -> tuple[str, dict[str, Any]]:
    """The ownership half of the boundary, as SQL rather than a post-filter.

    Row-level security already makes another organisation's files invisible. This is the
    second boundary and it is a product rule: your basket is yours, and an administrator
    holding `basket:manage` can see the organisation's.

    Returned as a fragment and its parameters so every read applies it the same way —
    the alternative is one query that forgets, which is how "employees can see each
    other's files" ships.
    """
    if actor.can(Permission.BASKET_MANAGE):
        return "true", {}
    return "owner_user_id = :owner", {"owner": str(actor.user_id)}


async def start_upload(
    session: AsyncSession,
    *,
    actor: Principal,
    store: ObjectStore | None,
    filename: str,
    content_type: str,
    size_bytes: int,
) -> UploadTicket:
    """Reserve a row and hand back a capability for exactly one object.

    The row exists BEFORE the bytes do, deliberately: `uploading` is the state that says
    "a URL was issued and nothing has arrived", which is what makes an abandoned upload
    recognisable rather than invisible. The bucket's lifecycle rule collects the object
    if one ever lands; this row is what the interface shows in the meantime.

    The type is decided here, before any bytes exist, so a format the deployment cannot
    do anything with is refused at the door rather than after a 500 MB transfer.
    """
    if store is None:
        raise ServiceUnavailable("File storage is not configured for this deployment.")

    if size_bytes <= 0:
        raise ValidationFailed("That file is empty.")
    if size_bytes > MAX_UPLOAD_BYTES:
        raise ValidationFailed(f"That file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if plan_for(content_type) is None:
        raise ValidationFailed("That kind of file cannot be added to a Knowledge Basket.")

    file_id = uuid4()
    key = object_key(actor.org_id, file_id)
    # Sanitised, not raw: Postgres refuses a NUL byte in a text column, so an
    # unsanitised original filename is a 500 rather than a stored file.
    original = sanitise_original(filename)

    await session.execute(
        text(
            "INSERT INTO basket_files "
            "(id, org_id, owner_user_id, object_key, original_filename, "
            " normalised_filename, declared_mime, size_bytes, state) "
            "VALUES (:id, :org, :owner, :key, :original, :normalised, :mime, :size, "
            " 'uploading')"
        ),
        {
            "id": file_id,
            "org": actor.org_id,
            "owner": actor.user_id,
            "key": key,
            "original": original,
            "normalised": normalise_filename(original),
            "mime": content_type,
            "size": size_bytes,
        },
    )

    return UploadTicket(
        file_id=file_id,
        upload=store.signed_upload(key, content_type=content_type, max_bytes=size_bytes),
    )


async def complete_upload(
    session: AsyncSession, *, actor: Principal, store: ObjectStore | None, file_id: UUID
) -> BasketFileRow:
    """Verify what actually landed, then set the pipeline going.

    **Nothing the client said about its own upload is taken on trust.** The size and
    checksum come from the store's own metadata, and the first bytes are sniffed: a file
    announced as a PDF whose content is a Windows executable is refused here, which is
    the whole reason a direct-to-storage upload needs a gate after it rather than before.

    A file whose type cannot yield text is `stored` — a terminal, successful state. It is
    kept, listed and downloadable, and no ingestion job is queued for it, because there
    is nothing to extract and pretending otherwise would leave it processing for ever.
    """
    if store is None:
        raise ServiceUnavailable("File storage is not configured for this deployment.")

    scope, params = _visible_to(actor)
    record = (
        await session.execute(
            text(
                f"SELECT id, object_key, declared_mime, state, original_filename "  # noqa: S608
                f"FROM basket_files WHERE id = :id AND deleted_at IS NULL AND {scope}"
            ),
            {"id": file_id, **params},
        )
    ).first()
    if record is None:
        raise NotFound("That file was not found.")
    if record.state != "uploading":
        # Completing twice is the browser retrying, not an error worth alarming about —
        # but it must not re-enqueue or re-verify.
        raise Conflict("That upload has already been completed.")

    measured = store.stat(record.object_key)
    if measured is None:
        raise ValidationFailed("The file did not finish uploading. Try again.")
    size, checksum = measured

    head = store.read_head(record.object_key)
    detected = sniff_mime(head)
    declared = record.declared_mime
    resolved = _resolve(declared=declared, detected=detected)

    if resolved is None:
        await _reject(
            session,
            file_id=file_id,
            reason=(
                "The contents of that file do not match the kind of file it claims to "
                "be, so it was not accepted."
            ),
            kind="mime_mismatch",
        )
        logger.warning(
            "%s",
            {"event": "basket_upload_rejected", "reason": "mime_mismatch", "file": str(file_id)},
        )
        return await _read_one(session, actor=actor, file_id=file_id)

    plan = plan_for(resolved)
    # `plan_for` already answered at `start_upload`; a None here means the resolved type
    # differs from the declared one in a way nothing accepts.
    if plan is None:
        await _reject(
            session,
            file_id=file_id,
            reason="That kind of file cannot be added to a Knowledge Basket.",
            kind="unsupported_type",
        )
        return await _read_one(session, actor=actor, file_id=file_id)

    state = "uploaded" if plan.mode == "text" else "stored"
    await session.execute(
        text(
            "UPDATE basket_files SET state = :state, size_bytes = :size, "
            "checksum_crc32c = :crc, detected_mime = :detected, "
            "failure_reason = :reason, updated_at = now() WHERE id = :id"
        ),
        {
            "id": file_id,
            "state": state,
            "size": size,
            "crc": checksum,
            "detected": resolved,
            # A stored-only file carries its explanation here so the interface can say
            # why it is not searchable without re-deriving it from the MIME type.
            "reason": plan.reason,
        },
    )

    if state == "uploaded":
        await _enqueue_ingest(session, org_id=actor.org_id, file_id=file_id)

    return await _read_one(session, actor=actor, file_id=file_id)


def _resolve(*, declared: str, detected: str | None) -> str | None:
    """What the file IS, given what it claims and what its bytes say.

    Three cases, and the middle one is where the container formats live:

      * the bytes identify themselves and agree with the claim — take it;
      * the bytes say `application/zip` and the claim is an OOXML type — take the claim,
        because every `.docx`, `.pptx` and `.xlsx` IS a zip and the signature cannot
        distinguish them. The readers refuse a zip that is not what it claims, so the
        lie is caught one step later rather than here;
      * the bytes identify themselves as something else entirely — refuse. This is the
        `.exe` announced as `image/png`.

    Text formats have no signature at all, so `detected is None` with a text claim is
    normal and accepted.
    """
    if detected is None:
        # No magic number. Only the formats that genuinely have none may pass.
        return declared if declared.startswith("text/") else None
    if detected == declared:
        return declared
    if detected == "application/zip" and _is_ooxml(declared):
        return declared
    # A declared text type whose bytes are something else is a lie worth refusing.
    return None


def _is_ooxml(mime: str) -> bool:
    return mime.startswith("application/vnd.openxmlformats-officedocument") or mime.startswith(
        "application/vnd.ms-excel.sheet"
    )


async def _reject(session: AsyncSession, *, file_id: UUID, reason: str, kind: str) -> None:
    await session.execute(
        text(
            "UPDATE basket_files SET state = 'rejected', failure_reason = :reason, "
            "failure_kind = :kind, updated_at = now() WHERE id = :id"
        ),
        {"id": file_id, "reason": reason, "kind": kind},
    )


async def _enqueue_ingest(session: AsyncSession, *, org_id: UUID, file_id: UUID) -> None:
    """Queue the extraction, on the same durable queue everything else uses.

    The idempotency key is the document key `run_document_job` would build, so a second
    completion of the same file cannot produce a second job — and a re-upload that
    genuinely changed the bytes reopens the completed row rather than inserting beside it.
    """
    source_id = await _basket_source(session, org_id=org_id)
    key = f"ingest.document:{org_id}:{source_id}:{file_id}"
    payload = f'{{"source_id": "{source_id}", "external_id": "{file_id}"}}'

    inserted = (
        await session.execute(
            text(
                "INSERT INTO jobs (id, org_id, kind, state, idempotency_key, payload_json) "
                "VALUES (:id, :org, 'ingest.document', 'pending', :key, cast(:payload AS jsonb)) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id"
            ),
            {"id": uuid4(), "org": str(org_id), "key": key, "payload": payload},
        )
    ).first()

    if inserted is None:
        # The key exists from an earlier version of this file. Reopen it rather than
        # leaving the new bytes unindexed behind a completed job.
        await session.execute(
            text(
                "UPDATE jobs SET state = 'pending', attempts = 0, locked_until = NULL, "
                "next_attempt_at = NULL, error = NULL, failure_kind = NULL, "
                "updated_at = now() WHERE idempotency_key = :key "
                "AND state IN ('completed', 'failed', 'dead_letter')"
            ),
            {"key": key},
        )


async def _basket_source(session: AsyncSession, *, org_id: UUID) -> UUID:
    """The organisation's single basket source row, created on first use.

    One per organisation rather than one per employee: `document_acl` is what separates
    people, and a source per employee would multiply rows for no isolation the ACL does
    not already provide.
    """
    existing = (
        await session.execute(text("SELECT id FROM sources WHERE system = 'basket' LIMIT 1"))
    ).first()
    if existing is not None:
        return UUID(str(existing.id))

    source_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO sources (id, org_id, system, config_json) "
            "VALUES (:id, :org, 'basket', cast(:config AS jsonb))"
        ),
        # `sources` carries no display name — the console labels a source from its
        # `system`. The label lives in `config_json` the way every provider source's
        # does, so nothing here invents a column.
        {"id": source_id, "org": org_id, "config": '{"label": "Knowledge Basket"}'},
    )
    return source_id


async def _read_one(session: AsyncSession, *, actor: Principal, file_id: UUID) -> BasketFileRow:
    scope, params = _visible_to(actor)
    record = (
        await session.execute(
            text(
                f"SELECT {_COLUMNS} FROM basket_files "  # noqa: S608
                f"WHERE id = :id AND deleted_at IS NULL AND {scope}"
            ),
            {"id": file_id, **params},
        )
    ).first()
    if record is None:
        raise NotFound("That file was not found.")
    return _row(record)


async def list_files(
    session: AsyncSession,
    *,
    actor: Principal,
    query: str | None = None,
    state: str | None = None,
    limit: int = 50,
) -> list[BasketFileRow]:
    """The caller's basket, newest first.

    Search is on `normalised_filename` so what the reader sorted by and what they search
    are the same string — a listing that sorts on one and matches on another is how a
    file appears to be missing.
    """
    scope, params = _visible_to(actor)
    filters = ["deleted_at IS NULL", scope]
    bound: dict[str, Any] = {"limit": max(1, min(limit, 200)), **params}

    if query:
        filters.append("normalised_filename LIKE :query")
        bound["query"] = f"%{normalise_filename(query)}%"
    if state:
        filters.append("state = :state")
        bound["state"] = state

    rows = (
        await session.execute(
            text(
                f"SELECT {_COLUMNS} FROM basket_files WHERE {' AND '.join(filters)} "  # noqa: S608
                "ORDER BY created_at DESC, id LIMIT :limit"
            ),
            bound,
        )
    ).all()
    return [_row(record) for record in rows]


async def download_url(
    session: AsyncSession, *, actor: Principal, store: ObjectStore | None, file_id: UUID
) -> str:
    """A short-lived GET, minted only after the row came back.

    The order is the security property: the query is scoped by RLS and by `_visible_to`,
    so a URL cannot be produced for a file the caller may not see. Deriving the key from
    the request instead of from the row is the mistake this function's shape prevents.
    """
    if store is None:
        raise ServiceUnavailable("File storage is not configured for this deployment.")

    scope, params = _visible_to(actor)
    record = (
        await session.execute(
            text(
                f"SELECT object_key, original_filename, state FROM basket_files "  # noqa: S608
                f"WHERE id = :id AND deleted_at IS NULL AND {scope}"
            ),
            {"id": file_id, **params},
        )
    ).first()
    if record is None:
        raise NotFound("That file was not found.")
    if record.state == "uploading":
        raise Conflict("That file has not finished uploading yet.")

    return store.signed_download(record.object_key, filename=record.original_filename)


async def rename_file(
    session: AsyncSession, *, actor: Principal, file_id: UUID, filename: str
) -> BasketFileRow:
    cleaned = sanitise_original(filename)
    if not cleaned or cleaned == "untitled":
        raise ValidationFailed("A file needs a name.")

    scope, params = _visible_to(actor)
    updated = (
        await session.execute(
            text(
                "UPDATE basket_files SET original_filename = :original, "  # noqa: S608
                "normalised_filename = :normalised, updated_at = now() "
                f"WHERE id = :id AND deleted_at IS NULL AND {scope} RETURNING id"
            ),
            {
                "id": file_id,
                "original": cleaned,
                "normalised": normalise_filename(cleaned),
                **params,
            },
        )
    ).first()
    if updated is None:
        raise NotFound("That file was not found.")
    return await _read_one(session, actor=actor, file_id=file_id)


async def delete_file(session: AsyncSession, *, actor: Principal, file_id: UUID) -> str:
    """Soft-delete, and stop the file being listed or downloadable immediately.

    The object survives until the lifecycle rule collects it, and the row survives as the
    record that the file existed. Hard-deleting here would destroy the audit trail and
    make the operation unrecoverable from a misclick.

    The document is deliberately NOT superseded: its chunks stop being reachable when the
    grant is removed, and unpicking a version chain because somebody tidied their basket
    is a far larger act than this button implies.
    """
    scope, params = _visible_to(actor)
    deleted = (
        await session.execute(
            text(
                "UPDATE basket_files SET deleted_at = now(), deleted_by = :actor, "  # noqa: S608
                f"updated_at = now() WHERE id = :id AND deleted_at IS NULL AND {scope} "
                "RETURNING original_filename"
            ),
            {"id": file_id, "actor": actor.user_id, **params},
        )
    ).first()
    if deleted is None:
        raise NotFound("That file was not found.")

    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) VALUES (:org, :actor, 'user', 'basket.file_deleted', "
            "'basket_file', :rid, 'success')"
        ),
        {"org": actor.org_id, "actor": str(actor.user_id), "rid": str(file_id)},
    )
    return str(deleted.original_filename)


async def retry_file(session: AsyncSession, *, actor: Principal, file_id: UUID) -> BasketFileRow:
    """Put a failed file back through extraction.

    Only `failed` is retryable. A `rejected` file's bytes were not what they claimed and
    a `quarantined` one was refused for a security reason — re-running either would fail
    identically, and offering the button would teach people it does nothing.
    """
    scope, params = _visible_to(actor)
    record = (
        await session.execute(
            text(
                "SELECT id, state FROM basket_files "  # noqa: S608
                f"WHERE id = :id AND deleted_at IS NULL AND {scope}"
            ),
            {"id": file_id, **params},
        )
    ).first()
    if record is None:
        raise NotFound("That file was not found.")
    if record.state not in _RETRYABLE:
        raise Conflict("That file cannot be retried.")

    await session.execute(
        text(
            "UPDATE basket_files SET state = 'uploaded', failure_reason = NULL, "
            "failure_kind = NULL, attempts = attempts + 1, updated_at = now() "
            "WHERE id = :id"
        ),
        {"id": file_id},
    )
    await _enqueue_ingest(session, org_id=actor.org_id, file_id=file_id)
    return await _read_one(session, actor=actor, file_id=file_id)
