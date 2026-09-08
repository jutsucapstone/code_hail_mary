"""Reading a Knowledge Basket file: one row from Postgres, one object from the bucket.

The connector in `jutsu_connectors.basket` needs a `BasketReader` and deliberately knows
nothing about either. This is that reader, and it lives in the worker because this is
where the database session and the bucket credentials already are — the same separation
`registry.py` keeps between choosing a connector and holding the credentials one needs.

**The ACL principal is built here, from the row.** `{namespace}:{subject}` with the
uploader's user id as the subject, so the grant `persist_document` writes names a person
the system can prove uploaded the file. Reading it from anywhere else — a job payload, a
request — would be an authorization input the caller could influence.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from jutsu_connectors.basket import BasketFile
from jutsu_core.storage import ObjectStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["PostgresBasketReader"]

#: The namespace half of the principal, matching `sources.system` and the value
#: `jutsu_api.basket.PRINCIPAL_NAMESPACE` writes. One string, two places, and a test
#: asserts they agree.
NAMESPACE = "basket"


class PostgresBasketReader:
    """The row and the bytes, for one organisation's basket.

    The session is the worker's, already scoped to the job's organisation by the GUC, so
    row-level security is what stops this reaching another tenant's file — no `org_id`
    predicate is written here, deliberately, for the reason `list_employees` gives.
    """

    def __init__(self, session: AsyncSession, store: ObjectStore | None) -> None:
        self._session = session
        self._store = store

    async def load(self, file_id: str) -> BasketFile | None:
        """The row, or None when it is gone.

        A soft-deleted file returns None so the connector raises `DocumentGone`, which
        `run_document_job` completes rather than fails — a file somebody deleted while
        its job was queued must not be retried five times against a row that will never
        come back.
        """
        try:
            identifier = UUID(file_id)
        except (ValueError, AttributeError):
            return None

        record = (
            await self._session.execute(
                text(
                    "SELECT id, object_key, original_filename, "
                    "coalesce(detected_mime, declared_mime) AS mime, "
                    "owner_user_id, created_at, size_bytes "
                    "FROM basket_files WHERE id = :id AND deleted_at IS NULL"
                ),
                {"id": identifier},
            )
        ).first()
        if record is None:
            return None

        return BasketFile(
            file_id=str(record.id),
            object_key=str(record.object_key),
            original_filename=str(record.original_filename),
            mime=str(record.mime),
            owner_principal=f"{NAMESPACE}:{record.owner_user_id}",
            uploaded_at=record.created_at,
            size_bytes=int(record.size_bytes),
        )

    async def read(self, key: str, *, max_bytes: int) -> bytes:
        """The object's bytes, bounded, off the event loop.

        `google-cloud-storage` is synchronous, so a download inside the loop would block
        every other coroutine in the worker for the length of the transfer — which for a
        thirty-megabyte file is long enough to matter to the drain running beside it.
        """
        if self._store is None:
            raise RuntimeError("object storage is not configured for this worker")
        return await asyncio.to_thread(self._store.download, key, max_bytes=max_bytes)
