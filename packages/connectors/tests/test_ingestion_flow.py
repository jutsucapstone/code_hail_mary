"""S3 into S4 into S5, on a real file (§9).

The first end-to-end exercise of the ingestion path that exists:

    LocalConnector.fetch  ->  RawDocument.body (ORIGINAL)
                          ->  mask()           (S4, masked text + offset map)
                          ->  chunk_document() (S5, masked text + ORIGINAL offsets)

Everything asserted here is an S4 or S5 invariant re-checked against input that came off
disk rather than out of a string literal. That is the point: the offset map was proven
against constructed text, and a mail body is the first real thing to go through it —
CRLF line endings, quoted replies, signature blocks and all.

Nothing is persisted. Writing any of this to Postgres is S8.
"""

from __future__ import annotations

from pathlib import Path

from corpus_support import Message, write_corpus
from jutsu_connectors.local import LocalConnector
from jutsu_core import chunk_document, content_hash_of, mask

BODY_WITH_PII = (
    "Team,\r\n"
    "\r\n"
    "Reach me on phillip.allen@example.com or +44 20 7946 0958 before Thursday.\r\n"
    "The card on file is 4111 1111 1111 1111 and the reference is 123-45-6789.\r\n"
    "\r\n"
    "> Confirmed for Thursday.\r\n"
    "> jane.taylor@example.com\r\n"
    "\r\n"
    "Nothing else here is sensitive.\r\n"
)

UNICODE_BODY = (
    "नमस्ते 🙏 यह पहला वाक्य है।\r\n"
    "\r\n"
    "Write to chair@example.com before Friday.\r\n"
    "\r\n"
    "और यह अंतिम पंक्ति है 🙏\r\n"
)


def corpus_with(body: str, root: Path) -> Path:
    return write_corpus(
        root,
        [
            Message(
                path="allen/sent/1",
                headers={
                    "Message-ID": "<a1@example.com>",
                    "Date": "Mon, 12 Jan 2026 09:15:00 -0600",
                    "From": "phillip.allen@example.com",
                    "To": "jane.taylor@example.com",
                    "Subject": "Falcon rollout",
                    "Content-Type": 'text/plain; charset="utf-8"',
                },
                body=body,
            )
        ],
    )


class TestMaskingALoadedDocument:
    async def test_the_body_reaches_masking_unmasked(self, tmp_path: Path) -> None:
        """S3 carries the original. Masking is downstream and offsets index this text."""
        root = corpus_with(BODY_WITH_PII, tmp_path / "maildir")
        document = await LocalConnector(root).fetch("allen/sent/1")
        assert "phillip.allen@example.com" in document.body
        assert "[EMAIL_" not in document.body

    async def test_masking_removes_the_pii_it_detects(self, tmp_path: Path) -> None:
        root = corpus_with(BODY_WITH_PII, tmp_path / "maildir")
        document = await LocalConnector(root).fetch("allen/sent/1")
        result = mask(document.body, namespace=document.external_id)

        for value in ("phillip.allen@example.com", "4111 1111 1111 1111", "123-45-6789"):
            assert value not in result.masked_text, value
        assert "Nothing else here is sensitive." in result.masked_text

    async def test_the_content_hash_is_of_the_original(self, tmp_path: Path) -> None:
        """§4.14 keys on content. Masking must not move the idempotency key."""
        root = corpus_with(BODY_WITH_PII, tmp_path / "maildir")
        document = await LocalConnector(root).fetch("allen/sent/1")
        assert document.content_hash == content_hash_of(document.body)

        masked = mask(document.body, namespace=document.external_id)
        assert content_hash_of(masked.masked_text) != document.content_hash


class TestChunkingALoadedDocument:
    async def test_offsets_resolve_against_the_original_body(self, tmp_path: Path) -> None:
        """The S5 invariant, re-proven on text that came off disk."""
        root = corpus_with(BODY_WITH_PII, tmp_path / "maildir")
        document = await LocalConnector(root).fetch("allen/sent/1")
        masked = mask(document.body, namespace=document.external_id)
        chunks = chunk_document(masked, target_tokens=24, min_tokens=5, overlap_ratio=0.0)

        assert chunks
        assert chunks[0].char_start == 0
        assert chunks[-1].char_end == len(document.body)
        for chunk in chunks:
            assert document.body[chunk.char_start : chunk.char_end]

    async def test_chunk_text_is_masked_text(self, tmp_path: Path) -> None:
        """The worst available bug in the pipeline: original PII into the vector store."""
        root = corpus_with(BODY_WITH_PII, tmp_path / "maildir")
        document = await LocalConnector(root).fetch("allen/sent/1")
        masked = mask(document.body, namespace=document.external_id)
        chunks = chunk_document(masked, target_tokens=24, min_tokens=5)

        for chunk in chunks:
            assert chunk.text in masked.masked_text
            for value in ("phillip.allen@example.com", "4111 1111 1111 1111"):
                assert value not in chunk.text

    async def test_the_chunks_cover_the_document(self, tmp_path: Path) -> None:
        root = corpus_with(BODY_WITH_PII, tmp_path / "maildir")
        document = await LocalConnector(root).fetch("allen/sent/1")
        masked = mask(document.body, namespace=document.external_id)
        chunks = chunk_document(masked, target_tokens=24, min_tokens=5)

        covered: set[int] = set()
        for chunk in chunks:
            covered.update(range(chunk.char_start, chunk.char_end))
        assert covered == set(range(len(document.body)))

    async def test_no_chunk_boundary_splits_a_masked_span(self, tmp_path: Path) -> None:
        root = corpus_with(BODY_WITH_PII, tmp_path / "maildir")
        document = await LocalConnector(root).fetch("allen/sent/1")
        masked = mask(document.body, namespace=document.external_id)
        chunks = chunk_document(masked, target_tokens=18, min_tokens=4, overlap_ratio=0.0)

        for chunk in chunks:
            assert chunk.text.count("[") == chunk.text.count("]")


class TestUnicodeThroughTheWholeChain:
    async def test_a_devanagari_message_survives_load_mask_and_chunk(self, tmp_path: Path) -> None:
        root = corpus_with(UNICODE_BODY, tmp_path / "maildir")
        document = await LocalConnector(root).fetch("allen/sent/1")
        assert "नमस्ते 🙏" in document.body

        masked = mask(document.body, namespace=document.external_id)
        chunks = chunk_document(masked, target_tokens=20, min_tokens=4, overlap_ratio=0.0)

        assert chunks[-1].char_end == len(document.body)
        assert len(document.body.encode("utf-8")) != len(document.body), (
            "the fixture must distinguish character and byte indices"
        )
        assert "chair@example.com" not in masked.masked_text


class TestDeterminismEndToEnd:
    async def test_the_whole_chain_is_reproducible(self, tmp_path: Path) -> None:
        """S3 through S5 with no random input anywhere. Ingestion is idempotent (§4.14)."""
        root = corpus_with(BODY_WITH_PII, tmp_path / "maildir")

        async def run() -> list[tuple[int, str, int, int]]:
            document = await LocalConnector(root).fetch("allen/sent/1")
            masked = mask(document.body, namespace=document.external_id)
            return [
                (chunk.ordinal, chunk.text, chunk.char_start, chunk.char_end)
                for chunk in chunk_document(masked, target_tokens=24, min_tokens=5)
            ]

        assert await run() == await run()

    async def test_the_masking_namespace_is_stable_for_one_document(self, tmp_path: Path) -> None:
        """Pseudonyms are scoped per document (ADR 0005); the external id is that scope."""
        root = corpus_with(BODY_WITH_PII, tmp_path / "maildir")
        document = await LocalConnector(root).fetch("allen/sent/1")

        first = mask(document.body, namespace=document.external_id)
        second = mask(document.body, namespace=document.external_id)
        assert first.masked_text == second.masked_text


class TestGrantsTravelWithTheDocument:
    async def test_acls_are_present_and_read_only(self, tmp_path: Path) -> None:
        """A chunk is only visible to a caller who can see the document's evidence (§4.6)."""
        root = corpus_with(BODY_WITH_PII, tmp_path / "maildir")
        document = await LocalConnector(root).fetch("allen/sent/1")

        assert document.acls
        assert all(entry.permission == "read" for entry in document.acls)
        assert {entry.principal_id for entry in document.acls} == {
            "local:phillip.allen@example.com",
            "local:jane.taylor@example.com",
        }
