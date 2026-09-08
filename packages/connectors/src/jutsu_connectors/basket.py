"""The Knowledge Basket as a connector, so uploaded files ingest like everything else.

`run_document_job` builds a connector from `sources.system` and calls
`connector.fetch(external_id)`. Satisfying that contract is what makes an uploaded file
reuse the whole existing pipeline — versioning by `content_hash`, masking, chunking, the
ACL write, the embedding job, the retry budget, the failure classification — instead of a
second ingestion path that would drift from it.

So the basket is a `sources` row with `system = 'basket'`, and `external_id` is the
`basket_files.id`. `fetch` downloads that object, extracts its text, and returns a
`RawDocument`. Everything downstream is the machinery every connector already uses.

**The grant is `owner_acl` and nothing wider.** An uploaded file is visible to the
principal of the person who uploaded it, exactly as a Drive document is visible to the
principal of the person who connected Drive. Sharing a basket file with a colleague is a
product decision that does not exist yet, and inventing a wider grant here would be the
"guess wearing an ACL" that `owner_acl`'s own docstring warns about.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from jutsu_core.models import AclEntry, RawDocument, SourceSystem

from jutsu_connectors.extraction import UnsupportedContent, extract, plan_for
from jutsu_connectors.providers.base import DocumentGone

__all__ = ["BasketConnector", "BasketFile", "BasketReader"]


@dataclass(frozen=True, slots=True)
class BasketFile:
    """What the worker needs to turn one stored object into a document.

    Read from `basket_files` by the caller, so this module needs no database session —
    which is what keeps it testable without one.
    """

    file_id: str
    object_key: str
    original_filename: str
    mime: str
    owner_principal: str
    uploaded_at: datetime
    size_bytes: int


class BasketReader(Protocol):
    """How the connector reaches a row and its bytes.

    A protocol rather than a concrete dependency: the worker supplies one backed by
    Postgres and Cloud Storage, and the tests supply one backed by a dict. Neither the
    database nor the bucket appears in this module.
    """

    async def load(self, file_id: str) -> BasketFile | None: ...

    async def read(self, key: str, *, max_bytes: int) -> bytes: ...


class BasketConnector:
    """One organisation's uploaded files, as a source.

    `walk` is deliberately not the listing the other connectors implement. Files arrive
    by upload, not by polling — the API enqueues an `ingest.document` job the moment an
    upload is verified — so there is no window to re-list and no cursor to advance.
    Returning nothing is the honest answer to "what is new since I last looked", because
    the answer is always "you were told".
    """

    #: Part of the `Connector` protocol: the ACL namespace this source's documents land
    #: in, and what `sources.system` holds for the row that produced them.
    system = SourceSystem.BASKET

    def __init__(self, reader: BasketReader) -> None:
        self._reader = reader

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]:
        """Nothing. See the class docstring — the basket is push, not poll.

        Written as an empty `for` rather than `return` followed by an unreachable
        `yield`: both make the function an async generator, and only this one is honest
        about it to a reader and to mypy.
        """
        empty: tuple[str, ...] = ()
        for identifier in empty:
            yield identifier

    async def fetch(self, external_id: str) -> RawDocument:
        """One uploaded file, as text the pipeline can chunk.

        Three ways this ends, and each is a different thing downstream:

          * a `RawDocument` — the file yielded text and becomes searchable;
          * `DocumentGone` — the row was deleted between the job being queued and run,
            which `run_document_job` completes rather than fails, so a deleted file does
            not sit in the queue being retried;
          * `UnsupportedContent` — the bytes could not be read. The caller records it
            against the file so the employee is told, rather than the job retrying a
            parse that will fail identically five times.
        """
        record = await self._reader.load(external_id)
        if record is None:
            raise DocumentGone(f"basket file {external_id} is no longer present")

        plan = plan_for(record.mime)
        if plan is None or plan.mode != "text":
            # Reached only if a file's plan changed between upload and ingestion; the
            # API does not enqueue a job for a stored-only file at all.
            raise UnsupportedContent("That file type is stored but not searchable.")

        from jutsu_connectors.extraction import MAX_EXTRACT_BYTES

        data = await self._reader.read(record.object_key, max_bytes=MAX_EXTRACT_BYTES)
        text = extract(data, mime=record.mime)

        return RawDocument(
            external_id=record.file_id,
            source_system=SourceSystem.BASKET,
            # No URI: the object is reachable only through a signed URL minted per
            # request after an authorization check, so there is no durable address to
            # record — and recording the bucket path would put an internal locator in a
            # row the console renders.
            uri=None,
            title=record.original_filename,
            body=text.text,
            mime=record.mime,
            author_external_id=record.owner_principal,
            created_at=record.uploaded_at,
            modified_at=record.uploaded_at,
            acls=self._grant(record),
            raw_metadata={
                "basket_file_id": record.file_id,
                "size_bytes": record.size_bytes,
                "truncated": text.truncated,
            },
        )

    async def acls(self, external_id: str) -> list[AclEntry]:
        """The grant for one file, without reading its bytes.

        Part of the `Connector` protocol and genuinely used: the pipeline captures ACLs
        as its own stage, and re-deriving them from a full `fetch` would download the
        object twice.
        """
        record = await self._reader.load(external_id)
        if record is None:
            raise DocumentGone(f"basket file {external_id} is no longer present")
        return self._grant(record)

    @staticmethod
    def _grant(record: BasketFile) -> list[AclEntry]:
        """The uploader's own principal, and nobody else's.

        Deliberately not `owner_acl` from `providers.base`: that builds the principal
        from a `ProviderContext`, which a basket file has no equivalent of. The SHAPE is
        identical — `{namespace}:{subject}` — and the constraint it encodes is the same
        one: a grant may only name a principal the system can prove, and the only
        provable principal here is the person who uploaded the file.
        """
        return [AclEntry(principal_type="user", principal_id=record.owner_principal)]

    async def aclose(self) -> None:
        """Nothing to release. Present because `close_connector` looks for it."""
        return


def basket_principal(namespace: str, subject: str) -> str:
    """`{source_system}:{subject}`, the namespaced form every ACL principal takes.

    Spelled here rather than inline so the one place a basket grant is constructed and
    the one place it is matched cannot drift apart (ADR 0010).
    """
    return f"{namespace}:{subject}"
