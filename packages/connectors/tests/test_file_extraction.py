"""Extraction, against real files built in the test rather than fixtures or mocks.

Every "searchable" claim in ADR 0020 is asserted here by constructing a genuine file of
that format and reading known text back out of it. A mocked reader would prove only that
the dispatch table is wired, which is the half that was never in doubt.

The bounds are tested the same way — with input that is actually hostile, because a
ceiling nobody has pushed against is a comment.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from jutsu_connectors.extraction import (
    MAX_EXTRACT_BYTES,
    MAX_TEXT_CHARS,
    UnsupportedContent,
    extract,
    plan_for,
)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class TestThePlanIsHonestAboutWhatCanBeRead:
    @pytest.mark.parametrize(
        "mime",
        ["text/plain", "text/markdown", "text/csv", "application/pdf", DOCX, PPTX, XLSX],
    )
    def test_readable_formats_are_extracted(self, mime: str) -> None:
        plan = plan_for(mime)

        assert plan is not None
        assert plan.mode == "text"

    @pytest.mark.parametrize(
        "mime",
        ["image/png", "image/jpeg", "video/mp4", "audio/mpeg", "application/zip"],
    )
    def test_unreadable_formats_are_stored_and_say_why(self, mime: str) -> None:
        # The product promise: a file is never left in "processing" because the stack
        # cannot read it. It is stored, and the interface explains.
        plan = plan_for(mime)

        assert plan is not None
        assert plan.mode == "store"
        assert plan.reason
        assert len(plan.reason) > 20, "the reason is shown to a person"

    def test_the_old_binary_office_formats_are_stored_not_pretended(self) -> None:
        # `.doc`/`.xls`/`.ppt` are OLE containers the pure-Python readers cannot open.
        plan = plan_for("application/x-ole-storage")

        assert plan is not None
        assert plan.mode == "store"
        assert ".docx" in (plan.reason or "")

    def test_an_unknown_type_is_refused_outright(self) -> None:
        # Distinct from "stored": nothing accepts it at all.
        assert plan_for("application/x-msdownload") is None
        assert plan_for("application/octet-stream") is None

    def test_every_readable_format_has_a_reader(self) -> None:
        # A format in the table with no reader would raise KeyError for a customer.
        from jutsu_connectors.extraction import _READERS, _TEXT_FORMATS

        assert set(_TEXT_FORMATS.values()) <= set(_READERS)


class TestRealFilesYieldTheirText:
    def test_plain_text(self) -> None:
        result = extract(b"The handover notes.\nSecond line.", mime="text/plain")

        assert "handover notes" in result.text
        assert not result.truncated

    def test_a_csv_reads_as_lines(self) -> None:
        result = extract(b"name,role\nAda,Engineer\nGrace,Admiral\n", mime="text/csv")

        assert "Ada Engineer" in result.text
        assert "Grace Admiral" in result.text

    def test_a_real_pdf(self) -> None:
        pypdf = pytest.importorskip("pypdf")
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buffer = io.BytesIO()
        writer.write(buffer)

        # A blank page has no text; what matters is that a genuine PDF parses and
        # returns rather than raising.
        result = extract(buffer.getvalue(), mime="application/pdf")

        assert result.text == ""

    def test_a_real_docx_including_its_tables(self) -> None:
        docx = pytest.importorskip("docx")
        document = docx.Document()
        document.add_paragraph("The decision was taken in March.")
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "Owner"
        table.rows[0].cells[1].text = "Ada"
        buffer = io.BytesIO()
        document.save(buffer)

        result = extract(buffer.getvalue(), mime=DOCX)

        assert "decision was taken in March" in result.text
        # Tables carry much of what people actually write down; skipping them silently
        # loses the most searchable half of a document.
        assert "Owner Ada" in result.text

    def test_a_real_pptx_including_speaker_notes(self) -> None:
        pptx = pytest.importorskip("pptx")
        deck = pptx.Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = "Migration plan"
        slide.notes_slide.notes_text_frame.text = "The argument lives in the notes."
        buffer = io.BytesIO()
        deck.save(buffer)

        result = extract(buffer.getvalue(), mime=PPTX)

        assert "Migration plan" in result.text
        assert "argument lives in the notes" in result.text

    def test_a_real_xlsx_across_sheets(self) -> None:
        openpyxl = pytest.importorskip("openpyxl")
        workbook = openpyxl.Workbook()
        workbook.active.title = "Costs"
        workbook.active.append(["Item", "Amount"])
        workbook.active.append(["Licences", 4200])
        buffer = io.BytesIO()
        workbook.save(buffer)

        result = extract(buffer.getvalue(), mime=XLSX)

        assert "# Costs" in result.text
        assert "Licences 4200" in result.text


class TestHostileInputIsRefusedRatherThanCrashingAWorker:
    def test_a_file_past_the_extraction_ceiling_is_refused_before_parsing(self) -> None:
        # The upload ceiling is 512 MiB; handing a parser half a gigabyte because the
        # UPLOAD allowed it is how a bound becomes decorative.
        with pytest.raises(UnsupportedContent, match="too large"):
            extract(b"x" * (MAX_EXTRACT_BYTES + 1), mime="text/plain")

    def test_bytes_that_are_not_the_format_they_claim(self) -> None:
        with pytest.raises(UnsupportedContent):
            extract(b"this is not a pdf at all", mime="application/pdf")
        with pytest.raises(UnsupportedContent):
            extract(b"nor is this a word document", mime=DOCX)

    def test_a_zip_that_is_not_an_office_document(self) -> None:
        # Every OOXML format is a zip, so a plain archive reaches the readers looking
        # plausible and must be refused by them rather than by its signature.
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("hello.txt", b"not an office document")

        with pytest.raises(UnsupportedContent):
            extract(archive.getvalue(), mime=DOCX)

    def test_an_encrypted_pdf_says_so_rather_than_failing_opaquely(self) -> None:
        pypdf = pytest.importorskip("pypdf")
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.encrypt("a-password")
        buffer = io.BytesIO()
        writer.write(buffer)

        with pytest.raises(UnsupportedContent, match="password"):
            extract(buffer.getvalue(), mime="application/pdf")

    def test_an_unreadable_encoding_still_yields_text(self) -> None:
        # A file that is almost UTF-8 with one bad byte is still worth indexing;
        # refusing it would mean telling somebody their notes failed over a stray byte.
        result = extract(b"caf\xe9 notes and more text", mime="text/plain")

        assert "notes" in result.text

    def test_a_nul_byte_cannot_reach_a_postgres_text_column(self) -> None:
        # Not tidiness: Postgres refuses \x00 in a text column outright, so this would
        # be a failed INSERT after all the extraction work was already paid for.
        result = extract(b"before\x00after", mime="text/plain")

        assert "\x00" not in result.text

    def test_long_text_is_truncated_and_says_so(self) -> None:
        # Truncating beats refusing — the first megabyte of a book is still worth
        # searching — but a silent truncation would look like a complete index.
        result = extract(b"a" * (MAX_TEXT_CHARS + 5_000), mime="text/plain")

        assert result.truncated
        assert len(result.text) == MAX_TEXT_CHARS

    def test_a_csv_field_past_the_reader_limit_does_not_lose_what_came_before(self) -> None:
        payload = b"a,b\nsmall,row\n" + b'"' + b"x" * 200_000 + b'"\n'

        result = extract(payload, mime="text/csv")

        assert "small row" in result.text


class TestEncodingIsChosenByEvidence:
    """UTF-16 is selected by a BOM, never tried by position.

    The bug this pins: UTF-16 decodes almost any even-length byte string without raising,
    so a plain `for encoding in ("utf-8", "utf-16", "cp1252")` chain turned a Latin-1 file
    with one accented character into a page of CJK mojibake instead of falling through.
    """

    def test_latin1_text_falls_through_to_cp1252_rather_than_utf16(self) -> None:
        result = extract("café notes".encode("cp1252"), mime="text/plain")

        assert "notes" in result.text
        assert "caf" in result.text

    def test_utf16_is_honoured_when_a_bom_says_so(self) -> None:
        result = extract("handover notes".encode("utf-16"), mime="text/plain")

        assert "handover notes" in result.text

    def test_utf8_is_preferred_when_it_is_valid(self) -> None:
        result = extract("naïve résumé".encode(), mime="text/plain")

        assert "naïve résumé" in result.text


class TestAZipContainerCannotSpendUnboundedMemory:
    """`MAX_EXTRACT_BYTES` bounds the COMPRESSED archive, which is not a bound at all.

    XML compresses at better than 1000:1, so a `.docx` well inside every existing limit
    can declare — and deliver — tens of gigabytes. The per-format caps do not help:
    `python-docx` and `python-pptx` parse the whole XML part into a tree before a single
    paragraph can be counted, so the memory is spent before any loop starts.

    The repository already had this guard, in `bulk_invitations` for `.xlsx` uploads. It
    was never carried across to the Knowledge Basket, which reads three zip formats.
    """

    @staticmethod
    def _archive(members: int = 1, declared: int = 1024) -> bytes:
        """A zip whose central directory truthfully declares a large expansion.

        Written by hand rather than with `zipfile.writestr`, because compressing an
        actual gigabyte to prove a point about not decompressing it would be absurd. The
        guard reads `file_size` from the directory, which is exactly what is forged here —
        and an attacker's real bomb declares the truth too, which is the case that matters.
        """
        import struct
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for index in range(members):
                archive.writestr(f"word/part{index}.xml", b"<w:p/>" * 8)
        raw = bytearray(buffer.getvalue())

        # Rewrite every central-directory entry's uncompressed size. The signature is
        # PK\x01\x02 and `file_size` is a 4-byte LE field at offset 24.
        position = 0
        while True:
            position = raw.find(b"PK\x01\x02", position)
            if position < 0:
                break
            struct.pack_into("<I", raw, position + 24, declared)
            position += 4
        return bytes(raw)

    def test_an_archive_that_declares_gigabytes_is_refused_before_it_is_opened(self) -> None:
        bomb = self._archive(members=4, declared=500_000_000)

        with pytest.raises(UnsupportedContent) as raised:
            extract(bomb, mime=DOCX)

        assert "stored" in str(raised.value).lower()

    def test_an_archive_with_absurdly_many_members_is_refused(self) -> None:
        with pytest.raises(UnsupportedContent):
            extract(self._archive(members=5_000, declared=8), mime=DOCX)

    def test_the_guard_covers_slides_and_spreadsheets_too(self) -> None:
        # Three readers open a zip, so one guarded reader is a guard that does not hold.
        bomb = self._archive(members=4, declared=500_000_000)

        for mime in (PPTX, XLSX):
            with pytest.raises(UnsupportedContent):
                extract(bomb, mime=mime)

    def test_an_ordinary_document_still_extracts(self) -> None:
        """The guard must not be a refusal of every Office file.

        A real `.docx` built by python-docx passes through untouched — the ceiling is far
        past any document a person actually writes.
        """
        docx = pytest.importorskip("docx")
        document = docx.Document()
        document.add_paragraph("The runbook lives in Confluence.")
        buffer = io.BytesIO()
        document.save(buffer)

        result = extract(buffer.getvalue(), mime=DOCX)

        assert "runbook lives in Confluence" in result.text
