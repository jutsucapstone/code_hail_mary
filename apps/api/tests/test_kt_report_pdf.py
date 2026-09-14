"""The handover PDF, parsed back rather than trusted (ADR 0025).

A test that checked a renderer was called would pass with an empty file. These build a
report, render it, and read the bytes back with `pypdf` — the same way a person's PDF
viewer would — then assert what a recipient would actually see: every section, the
difference between "nothing here yet" and "not in this package", the references, the
page numbers, and the absence of anything that must never reach paper.

No database and no model: `render_handover_pdf` is pure, which is what makes this
exhaustive rather than one happy path. What it is rendered FROM — the subject's evidence,
through the package's scope — is proven against Postgres in `test_kt_subject_scope.py`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from io import BytesIO

from jutsu_api.kt_report import (
    REPORT_TITLE,
    SECTION_EMPTY,
    SECTION_INCLUDED,
    SECTION_OUT_OF_SCOPE,
    HandoverReport,
    ReportItem,
    ReportSection,
    ReportSource,
    render_handover_pdf,
    report_filename,
)
from pypdf import PdfReader

NOW = datetime(2026, 9, 14, 9, 30, tzinfo=UTC)

ATLAS = ReportItem(
    headline="Atlas — the ledger migration",
    quote="Atlas moves the ledger to PostgreSQL, owned by the leaver.",
    date="2026-09-01",
    source=1,
)


def report(**overrides: object) -> HandoverReport:
    fields: dict[str, object] = {
        "subject_name": "Leaver Name",
        "subject_role": "Staff Engineer · Senior",
        "period_start": NOW - timedelta(days=90),
        "period_end": NOW,
        "expires_at": NOW + timedelta(days=30),
        "generated_at": NOW,
        "summary": "The leaver owned the Atlas migration [1].",
        "sections": (
            ReportSection("projects", "Key projects", SECTION_INCLUDED, (ATLAS,)),
            ReportSection("responsibilities", "Responsibilities", SECTION_EMPTY, ()),
            ReportSection("people", "Key contacts", SECTION_OUT_OF_SCOPE, ()),
            ReportSection("decisions", "Important decisions", SECTION_EMPTY, ()),
            ReportSection("meetings", "Meetings", SECTION_EMPTY, ()),
        ),
        "documents": ReportSection(
            "documents",
            "Important documents",
            SECTION_INCLUDED,
            (ReportItem("Atlas migration plan", "", "2026-09-01", 1),),
        ),
        "sources": (ReportSource(1, "Atlas migration plan", "gmail", "2026-09-01"),),
        "claims_considered": 1,
    }
    fields.update(overrides)
    return HandoverReport(**fields)  # type: ignore[arg-type]


def read(pdf: bytes) -> tuple[PdfReader, str]:
    """The pages' text, whitespace-normalised. A PDF's text layer breaks wherever the page
    wrapped, and a sentence split across two lines is still the sentence a reader sees."""
    reader = PdfReader(BytesIO(pdf))
    return reader, " ".join(" ".join(page.extract_text() for page in reader.pages).split())


class TestItIsARealDocument:
    def test_the_bytes_are_a_pdf_with_its_own_metadata(self) -> None:
        pdf = render_handover_pdf(report())
        reader, _ = read(pdf)

        assert pdf.startswith(b"%PDF-")
        assert pdf.rstrip().endswith(b"%%EOF")
        assert reader.metadata is not None
        assert reader.metadata.title == REPORT_TITLE
        assert reader.metadata.author == "JUTSU"

    def test_every_page_is_numbered_against_the_total(self) -> None:
        many = tuple(
            ReportItem(f"Project {n}", "A verbatim passage long enough to take room. " * 3, None, 1)
            for n in range(60)
        )
        sections = (ReportSection("projects", "Key projects", SECTION_INCLUDED, many),)
        reader, text = read(render_handover_pdf(report(sections=sections)))

        total = len(reader.pages)
        assert total > 1, "sixty items must not fit on one page"
        for number in range(1, total + 1):
            assert f"Page {number} of {total}" in text

    def test_the_branding_and_title_are_on_every_page(self) -> None:
        many = tuple(ReportItem(f"Item {n}", "passage " * 30, None, 1) for n in range(40))
        reader, _ = read(
            render_handover_pdf(
                report(
                    sections=(ReportSection("projects", "Key projects", SECTION_INCLUDED, many),)
                )
            )
        )

        for page in reader.pages:
            page_text = page.extract_text()
            assert "JUTSU" in page_text
            assert REPORT_TITLE in page_text


class TestWhatItSays:
    def test_the_required_sections_appear_in_reading_order(self) -> None:
        _, text = read(render_handover_pdf(report()))

        order = [
            REPORT_TITLE,
            "Executive overview",
            "Key projects",
            "Responsibilities",
            "Key contacts",
            "Important decisions",
            "Meetings",
            "Important documents",
            "Current and open work",
            "Sources",
        ]
        positions = [text.index(heading) for heading in order]
        assert positions == sorted(positions)

    def test_the_subject_period_and_validity_are_stated(self) -> None:
        _, text = read(render_handover_pdf(report()))

        assert "Handover of Leaver Name (Staff Engineer · Senior)" in text
        assert "Knowledge period: 16 Jun 2026 to 14 Sep 2026" in text
        assert "Package open until 14 Oct 2026" in text
        assert "Generated 14 Sep 2026, 09:30 UTC" in text

    def test_a_claim_carries_its_verbatim_evidence_and_its_reference(self) -> None:
        _, text = read(render_handover_pdf(report()))

        assert "Atlas — the ledger migration" in text
        assert "Atlas moves the ledger to PostgreSQL, owned by the leaver." in text
        assert "[1] Atlas migration plan" in text
        assert "The leaver owned the Atlas migration [1]." in text

    def test_empty_and_out_of_scope_are_two_different_statements(self) -> None:
        """ "Nothing here yet" and "not part of this package" are different facts, and a
        recipient acts on them differently — so they are never collapsed into one."""
        _, text = read(render_handover_pdf(report()))

        assert "Not part of this package's scope." in text
        assert "No evidence for this section in the package yet." in text

    def test_a_list_of_people_says_how_it_is_ordered(self) -> None:
        """Non-negotiables 16-18: contacts are listed by most recent mention and never
        scored — and a list of people says so where it is shown, not only when empty."""
        contact = ReportItem(
            "Sarah Chen — owns the migration plan",
            "Sarah Chen owns the migration plan.",
            None,
            1,
        )
        sections = (ReportSection("people", "Key contacts", SECTION_INCLUDED, (contact,)),)
        _, text = read(render_handover_pdf(report(sections=sections)))

        assert "Listed by most recent mention, never ranked." in text
        assert "Sarah Chen owns the migration plan." in text

    def test_an_ungrounded_report_says_so_and_invents_no_overview(self) -> None:
        _, text = read(render_handover_pdf(report(summary=None)))

        assert "could not ground an executive summary" in text
        assert "The leaver owned" not in text

    def test_open_work_is_described_honestly_rather_than_fabricated(self) -> None:
        _, text = read(render_handover_pdf(report()))

        assert "does not extract open work as a category of its own" in text

    def test_markdown_the_model_emits_is_readable_not_literal(self) -> None:
        _, text = read(
            render_handover_pdf(report(summary="**Atlas** is the priority [1].\n\n- Ship it [1]"))
        )

        assert "**" not in text
        assert "• Ship it [1]" in text


class TestWhatItMustNeverSay:
    def test_no_internal_identifier_email_or_storage_path(self) -> None:
        _, text = read(render_handover_pdf(report()))

        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-", text), "a UUID reached paper"
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "an address reached paper"
        for internal in ("gs://", "KT-JUTSU-", "basket:", "local:"):
            assert internal not in text

    def test_markup_in_evidence_is_text_not_structure(self) -> None:
        """Evidence is untrusted text. A quote containing markup must print as characters,
        not break the document or style it."""
        hostile = ReportItem(
            "<b>Injected</b> & <font color='red'>styled</font>", "a < b & c > d", None, 1
        )
        sections = (ReportSection("projects", "Key projects", SECTION_INCLUDED, (hostile,)),)
        _, text = read(render_handover_pdf(report(sections=sections)))

        assert "<b>Injected</b>" in text
        assert "a < b & c > d" in text


class TestHeadingsStayWithTheirEntries:
    def test_a_heading_never_ends_a_page_without_its_first_entry(self) -> None:
        """A heading stranded at the foot of one page, its entries on the next, reads as a
        section with nothing in it. The overview grows a line at a time, so every heading
        after it is carried across the page end through each position it can occupy."""
        contact = ReportItem("Sarah Chen — owns the plan", "Sarah Chen owns the plan.", None, 1)
        decision = ReportItem("Queue stays self-hosted", "The queue stays self-hosted.", None, 1)
        projects = tuple(
            ReportItem(f"Workstream {n}", "Reconciliation runs nightly.", None, 1)
            for n in range(12)
        )
        sections = (
            ReportSection("projects", "Key projects", SECTION_INCLUDED, projects),
            ReportSection("people", "Key contacts", SECTION_INCLUDED, (contact,)),
            ReportSection("decisions", "Important decisions", SECTION_INCLUDED, (decision,)),
        )
        first_entry = {
            "Key projects": "Workstream 0",
            "Key contacts": "Sarah Chen — owns the plan",
            "Important decisions": "Queue stays self-hosted",
            "Important documents": "Atlas migration plan",
            "Current and open work": "does not extract open work",
            "Sources": "[1] Atlas migration plan",
        }

        for lines in range(48):
            summary = "\n\n".join(f"- Point {n} [1]" for n in range(lines)) or None
            pdf = render_handover_pdf(report(summary=summary, sections=sections))
            pages = [
                " ".join(page.extract_text().split()) for page in PdfReader(BytesIO(pdf)).pages
            ]

            for heading, entry in first_entry.items():
                page = next(number for number, text in enumerate(pages) if heading in text)
                assert entry in pages[page], (
                    f"{heading!r} ends page {page + 1} without its first entry "
                    f"({lines} overview lines)"
                )


class TestCharactersTheFontCannotDraw:
    def test_they_are_marked_and_the_document_says_so(self) -> None:
        item = ReportItem("Server सर्वर move", "Budget ₹5 lakh, naïve café", None, 1)
        sections = (ReportSection("projects", "Key projects", SECTION_INCLUDED, (item,)),)
        _, text = read(render_handover_pdf(report(sections=sections)))

        assert "INR 5 lakh" in text, "the rupee sign is transliterated, not dropped"
        assert "naïve café" in text, "Latin accents are drawn as themselves"
        assert "?" in text
        assert "shown as" in text and "The KT console shows the original text." in text

    def test_a_fully_drawable_report_carries_no_such_notice(self) -> None:
        _, text = read(render_handover_pdf(report()))

        assert "The KT console shows the original text." not in text


class TestTheFilename:
    def test_it_names_the_date_and_no_person(self) -> None:
        assert report_filename(NOW) == "jutsu-handover-summary-20260914.pdf"
