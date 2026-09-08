"""The basket as a source, asserted against the contract `run_document_job` calls.

What matters here is that an uploaded file produces a `RawDocument` the existing pipeline
can consume unchanged — the same shape a Drive document or a Jira issue produces — and
that the grant it carries names exactly one provable principal.

The reader is a dict. Neither Postgres nor Cloud Storage appears, which is the point of
`BasketReader` being a protocol: the mapping from an object to a document is pure, so it
can be tested exhaustively without credentials.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest
from jutsu_connectors.basket import BasketConnector, BasketFile
from jutsu_connectors.extraction import UnsupportedContent
from jutsu_connectors.providers.base import DocumentGone
from jutsu_core.models import SourceSystem

UPLOADED = datetime(2026, 9, 8, 10, 30, tzinfo=UTC)
PRINCIPAL = "basket:44444444-4444-4444-8444-444444444444"


class FakeReader:
    """A basket with whatever the test put in it."""

    def __init__(self, rows: dict[str, BasketFile], blobs: dict[str, bytes]) -> None:
        self.rows = rows
        self.blobs = blobs
        self.reads: list[tuple[str, int]] = []

    async def load(self, file_id: str) -> BasketFile | None:
        return self.rows.get(file_id)

    async def read(self, key: str, *, max_bytes: int) -> bytes:
        self.reads.append((key, max_bytes))
        return self.blobs[key]


def a_file(**overrides: object) -> BasketFile:
    defaults: dict[str, object] = {
        "file_id": "55555555-5555-4555-8555-555555555555",
        "object_key": "org/11111111-1111-4111-8111-111111111111/55555555-5555-4555-8555-555555555555",
        "original_filename": "handover notes.txt",
        "mime": "text/plain",
        "owner_principal": PRINCIPAL,
        "uploaded_at": UPLOADED,
        "size_bytes": 42,
    }
    defaults.update(overrides)
    return BasketFile(**defaults)  # type: ignore[arg-type]


def connector_for(record: BasketFile, body: bytes) -> tuple[BasketConnector, FakeReader]:
    reader = FakeReader({record.file_id: record}, {record.object_key: body})
    return BasketConnector(reader), reader


class TestAnUploadedFileBecomesADocument:
    async def test_it_produces_a_raw_document_the_pipeline_can_consume(self) -> None:
        record = a_file()
        connector, _ = connector_for(record, b"The migration was decided in March.")

        raw = await connector.fetch(record.file_id)

        assert raw.source_system is SourceSystem.BASKET
        # `external_id` is the basket row's id, which is what makes the document's
        # identity stable across re-uploads and what the partial unique index keys on.
        assert raw.external_id == record.file_id
        assert raw.title == "handover notes.txt"
        assert "migration was decided in March" in raw.body
        assert raw.created_at == UPLOADED

    async def test_it_carries_a_content_hash_so_versioning_works(self) -> None:
        # `persist_document` decides created/updated/unchanged by this alone.
        record = a_file()
        connector, _ = connector_for(record, b"same text")
        other, _ = connector_for(record, b"different text")

        first = await connector.fetch(record.file_id)
        second = await other.fetch(record.file_id)

        assert first.content_hash != second.content_hash
        assert first.content_hash == (await connector.fetch(record.file_id)).content_hash

    async def test_it_never_records_the_bucket_path(self) -> None:
        # The object is reachable only through a signed URL minted per request. Putting
        # the key in `uri` would place an internal locator in a row the console renders.
        record = a_file()
        connector, _ = connector_for(record, b"text")

        raw = await connector.fetch(record.file_id)

        assert raw.uri is None
        assert record.object_key not in str(raw.model_dump())

    async def test_the_read_is_bounded(self) -> None:
        # An extractor that can handle 32 MiB must not be handed 512 because the upload
        # ceiling allowed it.
        record = a_file()
        connector, reader = connector_for(record, b"text")

        await connector.fetch(record.file_id)

        assert reader.reads
        _, max_bytes = reader.reads[0]
        assert max_bytes == 32 * 1024 * 1024


class TestTheGrantNamesOneProvablePrincipal:
    async def test_it_grants_the_uploader_and_nobody_else(self) -> None:
        record = a_file()
        connector, _ = connector_for(record, b"text")

        raw = await connector.fetch(record.file_id)

        assert len(raw.acls) == 1
        assert raw.acls[0].principal_type == "user"
        assert raw.acls[0].principal_id == PRINCIPAL

    async def test_the_principal_is_namespaced(self) -> None:
        # Without the prefix a basket file id and a Slack member id share a string
        # space, and a grant from one system could authorize a principal from another
        # (ADR 0010).
        record = a_file()
        connector, _ = connector_for(record, b"text")

        raw = await connector.fetch(record.file_id)

        assert raw.acls[0].principal_id.startswith("basket:")

    async def test_a_different_uploader_gets_a_different_grant(self) -> None:
        other = "basket:99999999-9999-4999-8999-999999999999"
        record = a_file(owner_principal=other)
        connector, _ = connector_for(record, b"text")

        raw = await connector.fetch(record.file_id)

        assert raw.acls[0].principal_id == other


class TestTheThreeWaysFetchEnds:
    async def test_a_deleted_row_is_gone_rather_than_a_failure(self) -> None:
        """`DocumentGone` is completed by `run_document_job`, not failed.

        A file deleted between the job being queued and run must not sit in the queue
        being retried five times against a row that will never come back.
        """
        connector = BasketConnector(FakeReader({}, {}))

        with pytest.raises(DocumentGone):
            await connector.fetch("55555555-5555-4555-8555-555555555555")

    async def test_a_stored_only_type_is_refused_rather_than_pretended(self) -> None:
        # The API does not enqueue a job for one, so this is the belt: a file whose plan
        # changed between upload and ingestion must not silently produce an empty
        # document that looks searchable.
        record = a_file(mime="video/mp4")
        connector, _ = connector_for(record, b"\x00\x00\x00\x18ftypmp42")

        with pytest.raises(UnsupportedContent):
            await connector.fetch(record.file_id)

    async def test_unreadable_bytes_raise_rather_than_producing_an_empty_document(
        self,
    ) -> None:
        # An empty body would be indexed as a document with no text — worse than a
        # failure, because it looks like success and returns nothing for ever.
        record = a_file(mime="application/pdf")
        connector, _ = connector_for(record, b"this is not a pdf")

        with pytest.raises(UnsupportedContent):
            await connector.fetch(record.file_id)


class TestTheBasketIsPushNotPoll:
    async def test_list_since_lists_nothing(self) -> None:
        """Files arrive by upload, so there is no window to re-list and no cursor.

        Returning nothing is the honest answer to "what is new since I last looked" —
        the answer is always "you were told when it happened".
        """
        connector = BasketConnector(FakeReader({}, {}))

        listed = [identifier async for identifier in connector.list_since(None)]

        assert listed == []

    async def test_it_satisfies_the_connector_protocol(self) -> None:
        # `resolve_connector` returns it as a `Connector`, so the four members the
        # protocol names have to be there — a missing one is a runtime AttributeError
        # inside a job rather than a type error at the call site.
        connector = BasketConnector(FakeReader({}, {}))

        assert connector.system is SourceSystem.BASKET
        for member in ("list_since", "fetch", "acls"):
            assert callable(getattr(connector, member)), member

    async def test_acls_does_not_download_the_object(self) -> None:
        # The pipeline captures ACLs as its own stage; re-deriving them from a full
        # `fetch` would pay for the transfer twice.
        record = a_file()
        connector, reader = connector_for(record, b"text")

        grants = await connector.acls(record.file_id)

        assert [g.principal_id for g in grants] == [PRINCIPAL]
        assert reader.reads == []


class TestRealFormatsThroughTheConnector:
    async def test_a_real_docx_arrives_as_searchable_text(self) -> None:
        docx = pytest.importorskip("docx")
        document = docx.Document()
        document.add_paragraph("The runbook lives in Confluence.")
        buffer = io.BytesIO()
        document.save(buffer)

        record = a_file(
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            original_filename="runbook.docx",
        )
        connector, _ = connector_for(record, buffer.getvalue())

        raw = await connector.fetch(record.file_id)

        assert "runbook lives in Confluence" in raw.body
        assert raw.title == "runbook.docx"

    async def test_a_real_pdf_arrives_without_raising(self) -> None:
        pypdf = pytest.importorskip("pypdf")
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buffer = io.BytesIO()
        writer.write(buffer)

        record = a_file(mime="application/pdf", original_filename="scan.pdf")
        connector, _ = connector_for(record, buffer.getvalue())

        raw = await connector.fetch(record.file_id)

        # A PDF with no text layer yields an empty body. That is a real outcome the
        # caller records as "we could not read any text", not a crash.
        assert raw.body == ""
        assert raw.title == "scan.pdf"
