"""The handover report: one evidence-grounded PDF from one opened package (§29, ADR 0025).

"Compose summary" used to render a paragraph in the page and stop — the page's own
docstring called itself "§29 without a fake downloadable". This is the downloadable, built
so that it is not fake:

    POST /v1/kt/{code}/handover-report
      answers_configured ─► KT_SUMMARY budget ─► _open_for + _scope_for, ONCE
        ─► handover_summary_in_scope   the grounded narrative, and the claims it read
        ─► documents_in_scope          the documents section, when in scope
        ─► render_handover_pdf         bytes, in memory, never persisted
      ─► audit kt.handover_report (counts only)

Four rules, each the reason a shortcut was not taken.

**The same boundary as Ask KT.** Everything here reads through the `KtScope` this request
opened, so the PDF and the copilot cannot disagree about what the package holds, and the
recipient's own corpus is not an input to either.

**The server composes and renders in one request, and accepts no content from the
browser.** A PDF headed "Knowledge Transfer — Handover Summary", carrying JUTSU's name and
numbered references, is a document people forward and believe. An endpoint that rendered
text it was sent would print a convincing handover that says anything at all.

**Sections are the claims themselves, not generated prose.** Projects, responsibilities,
contacts, decisions and meetings list extracted claims with their verbatim, quote-gated
evidence. Only the executive overview is model-written, and it passes the same citation
gate every JUTSU answer passes. A section with nothing to show says so, and a section
outside the package's scope says THAT — two different facts, never collapsed into one.

**Nothing is stored.** Not the PDF, not the narrative: a stored handover would outlive the
package state that grounded it — the reason the summary itself was never persisted
(ADR 0016).
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.answers import AnswerOutcome, AnswerTransport
from jutsu_api.kt import (
    KtDocument,
    KtInsight,
    _open_for,
    _scope_for,
    _subject_profile,
    _touch_activity,
    documents_in_scope,
    handover_summary_in_scope,
)

__all__ = [
    "REPORT_TITLE",
    "HandoverReport",
    "ReportItem",
    "ReportSection",
    "ReportSource",
    "compose_handover_report",
    "render_handover_pdf",
    "report_filename",
]

logger = logging.getLogger("jutsu.api.kt")

REPORT_TITLE: Final = "Knowledge Transfer — Handover Summary"

#: How many of the subject's documents the report lists. A bibliography, not the corpus.
REPORT_DOCUMENTS: Final = 25

#: `(claim type, package category, section title)`, in the order a first day reads them.
_SECTIONS: Final[tuple[tuple[str, str, str], ...]] = (
    ("project", "projects", "Key projects"),
    ("responsibility", "responsibilities", "Responsibilities"),
    ("person", "people", "Key contacts"),
    ("decision", "decisions", "Important decisions"),
    ("meeting", "meetings", "Meetings"),
)

SECTION_INCLUDED: Final = "included"
SECTION_EMPTY: Final = "empty"
SECTION_OUT_OF_SCOPE: Final = "out_of_scope"

_MARKER: Final = re.compile(r"\[(\d{1,3})\]")


@dataclass(frozen=True, slots=True)
class ReportSource:
    """One entry in the report's reference list. A title and where it came from — never a
    document id, a chunk id or a storage location: a reader needs to recognise the source,
    and an internal identifier printed on paper helps nobody and outlives everything."""

    number: int
    title: str
    source_system: str
    date: str | None


@dataclass(frozen=True, slots=True)
class ReportItem:
    headline: str
    #: The verbatim evidence, exactly as the extraction quote gate verified it. Empty for a
    #: documents-section entry, which is its own evidence.
    quote: str
    date: str | None
    source: int


@dataclass(frozen=True, slots=True)
class ReportSection:
    key: str
    title: str
    state: str
    items: tuple[ReportItem, ...]


@dataclass(frozen=True, slots=True)
class HandoverReport:
    subject_name: str | None
    subject_role: str | None
    period_start: datetime | None
    period_end: datetime | None
    expires_at: datetime
    generated_at: datetime
    #: The grounded narrative with its markers renumbered onto `sources`, or None when the
    #: claims could not ground one.
    summary: str | None
    sections: tuple[ReportSection, ...]
    documents: ReportSection
    sources: tuple[ReportSource, ...]
    #: How many claims the summary and the sections stood on — they are the same list.
    claims_considered: int


class _References:
    """Numbers documents in the order they are first cited, one number per document."""

    def __init__(self) -> None:
        self._by_document: dict[str, ReportSource] = {}

    def number(
        self, document_id: object, title: str, source_system: str, when: datetime | None
    ) -> int:
        key = str(document_id)
        existing = self._by_document.get(key)
        if existing is not None:
            return existing.number
        source = ReportSource(
            number=len(self._by_document) + 1,
            title=title,
            source_system=source_system,
            date=when.strftime("%Y-%m-%d") if when is not None else None,
        )
        self._by_document[key] = source
        return source.number

    def all(self) -> tuple[ReportSource, ...]:
        return tuple(self._by_document.values())


def _headline(insight: KtInsight) -> str:
    if insight.name and insight.summary and insight.name != insight.summary:
        return f"{insight.name} — {insight.summary}"
    return insight.name or insight.summary or insight.quote[:160]


def _renumber(answer: str, mapping: dict[int, int]) -> str:
    """Move the narrative's `[n]` markers from the evidence list onto the report's sources.

    A renumbering, not an edit: marker `n` named evidence item `n`, which belongs to exactly
    one document, and that document has exactly one number in the reference list. The
    citation gate already guaranteed every marker names real evidence, so every marker has
    a mapping; one that somehow did not is left exactly as the model wrote it.
    """

    def swap(match: re.Match[str]) -> str:
        number = mapping.get(int(match.group(1)))
        return f"[{number}]" if number is not None else match.group(0)

    return _MARKER.sub(swap, answer)


def _role(profile: object) -> str | None:
    title = getattr(profile, "role_title", None) or getattr(profile, "designation", None)
    parts = [part for part in (title, getattr(profile, "role_level", None)) if part]
    return " · ".join(parts) or None


def report_filename(generated_at: datetime) -> str:
    """No name in the filename. A download list outlives the handover, and the subject is
    already named inside the document for anybody allowed to open it."""
    return f"jutsu-handover-summary-{generated_at.strftime('%Y%m%d')}.pdf"


async def compose_handover_report(
    session: AsyncSession,
    transport: AnswerTransport,
    *,
    org_id: UUID,
    user_id: UUID,
    kt_code: str,
    correlation_id: str | None = None,
) -> tuple[HandoverReport, AnswerOutcome]:
    """Everything the report says, from one open of one package.

    One `_open_for` and one `KT_OPEN` allowance for the whole report, one claims read shared
    by the narrative and the sections, one documents read, one model call.
    """
    started = time.monotonic()
    row = await _open_for(session, org_id=org_id, user_id=user_id, kt_code=kt_code)
    scope = await _scope_for(session, row)
    await _touch_activity(session, package_id=scope.package_id)
    logger.info("%s", {"event": "kt_summary_started", "package_id": str(scope.package_id)})

    profile = await _subject_profile(session, row)
    outcome, claims = await handover_summary_in_scope(
        session,
        transport,
        scope,
        org_id=org_id,
        user_id=user_id,
        action="kt.handover_report",
        correlation_id=correlation_id,
    )
    documents: list[KtDocument] = []
    if "documents" in scope.categories:
        documents = (
            await documents_in_scope(session, scope, limit=REPORT_DOCUMENTS, cursor=None)
        ).items

    when: dict[str, datetime] = {str(c.document_id): c.occurred_at for c in claims}
    references = _References()

    # The narrative's references first, in marker order, so its citations read 1, 2, 3.
    mapping = {
        citation.marker: references.number(
            citation.document_id,
            citation.document_title,
            citation.source_system,
            when.get(str(citation.document_id)),
        )
        for citation in sorted(outcome.citations, key=lambda c: c.marker)
    }
    summary = _renumber(outcome.answer, mapping) if outcome.answer else None

    sections: list[ReportSection] = []
    for claim_type, category, title in _SECTIONS:
        if category not in scope.categories:
            sections.append(ReportSection(category, title, SECTION_OUT_OF_SCOPE, ()))
            continue
        items = tuple(
            ReportItem(
                headline=_headline(claim),
                quote=claim.quote,
                date=claim.date,
                source=references.number(
                    claim.document_id, claim.document_title, claim.source_system, claim.occurred_at
                ),
            )
            for claim in claims
            if claim.claim_type == claim_type
        )
        sections.append(
            ReportSection(category, title, SECTION_INCLUDED if items else SECTION_EMPTY, items)
        )

    if "documents" in scope.categories:
        document_items = tuple(
            ReportItem(
                headline=document.title,
                quote="",
                date=document.created_at.strftime("%Y-%m-%d"),
                source=references.number(
                    document.id, document.title, document.source_system, document.created_at
                ),
            )
            for document in documents
        )
        document_section = ReportSection(
            "documents",
            "Important documents",
            SECTION_INCLUDED if document_items else SECTION_EMPTY,
            document_items,
        )
    else:
        document_section = ReportSection(
            "documents", "Important documents", SECTION_OUT_OF_SCOPE, ()
        )

    report = HandoverReport(
        subject_name=profile.display_name,
        subject_role=_role(profile),
        period_start=row.period_start,  # type: ignore[attr-defined]
        period_end=row.period_end,  # type: ignore[attr-defined]
        expires_at=row.expires_at,  # type: ignore[attr-defined]
        generated_at=datetime.now(tz=UTC),
        summary=summary,
        sections=tuple(sections),
        documents=document_section,
        sources=references.all(),
        claims_considered=len(claims),
    )
    logger.info(
        "%s",
        {
            "event": "kt_summary_completed",
            "package_id": str(scope.package_id),
            "claims": len(claims),
            "citations": len(outcome.citations),
            "sources": len(report.sources),
            "insufficient_evidence": outcome.insufficient_evidence,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return report, outcome


# ------------------------------------------------------------------------------ render

#: The light-theme brand green (spec amendment A2), and the neutrals a printed page needs.
_BRAND: Final = colors.HexColor("#499f02")
_INK: Final = colors.HexColor("#1b1d1a")
_MUTED: Final = colors.HexColor("#5f6660")
_HAIRLINE: Final = colors.HexColor("#d9dcd6")

_FONT: Final = "JutsuSans"

#: Characters a handover's evidence genuinely contains that the embedded font does not
#: draw, mapped to what they mean. Everything else missing becomes `?` and the report says
#: so on its last page — a silently dropped character in a name is a quiet falsehood.
_TRANSLITERATE: Final[dict[str, str]] = {
    "\u00a0": " ",  # no-break space
    "\u2009": " ",  # thin space
    "\u202f": " ",  # narrow no-break space: models emit it before citation markers
    "\u200b": "",  # zero-width space
    "\u200c": "",  # zero-width non-joiner
    "\u200d": "",  # zero-width joiner
    "\ufeff": "",  # byte-order mark
    "\u20b9": "INR ",  # rupee sign
}


#: Registration mutates reportlab's process-wide font table, and the router renders on a
#: worker thread so a PDF never blocks the event loop — two first renders at once must not
#: both register.
_FONT_LOCK: Final = threading.Lock()


def _register_fonts() -> set[int]:
    """Embed the Vera family reportlab ships, once, and return the codepoints it draws.

    A TrueType font rather than the PDF core fonts because the core fonts are Latin-1 and a
    handover's evidence is not. Vera is bundled with the dependency, so rendering needs no
    network, no system fonts and no font file in this repository.
    """
    with _FONT_LOCK:
        _register_family()
    font: Any = pdfmetrics.getFont(_FONT)
    return set(getattr(font.face, "charToGlyph", {}))


def _register_family() -> None:
    if _FONT not in pdfmetrics.getRegisteredFontNames():
        directory = Path(reportlab.__file__).parent / "fonts"
        for name, file in (
            (_FONT, "Vera.ttf"),
            (f"{_FONT}-Bold", "VeraBd.ttf"),
            (f"{_FONT}-Italic", "VeraIt.ttf"),
            (f"{_FONT}-BoldItalic", "VeraBI.ttf"),
        ):
            pdfmetrics.registerFont(TTFont(name, str(directory / file)))
        pdfmetrics.registerFontFamily(
            _FONT,
            normal=_FONT,
            bold=f"{_FONT}-Bold",
            italic=f"{_FONT}-Italic",
            boldItalic=f"{_FONT}-BoldItalic",
        )


class _Text:
    """Sanitises evidence and model text for one render, and remembers what it replaced."""

    def __init__(self, supported: set[int]) -> None:
        self._supported = supported
        self.replaced = False

    def __call__(self, value: str) -> str:
        out: list[str] = []
        for char in value:
            if char in _TRANSLITERATE:
                out.append(_TRANSLITERATE[char])
            elif char == "\n":
                out.append("\n")
            elif char == "\t":
                out.append(" ")
            elif not self._supported or ord(char) in self._supported:
                out.append(char)
            else:
                out.append("?")
                self.replaced = True
        # Paragraph markup is XML-like: `&`, `<` and `>` must be entities. Quotes need
        # nothing, and escaping them would print `&quot;` into a verbatim quote.
        return html.escape("".join(out), quote=False)


class _NumberedCanvas(Canvas):
    """Header, footer and "Page X of Y", drawn once the total is known."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pages: list[dict[str, Any]] = []

    def showPage(self) -> None:
        self._pages.append(dict(self.__dict__))
        # reportlab's own page reset; the standard "Page X of Y" recipe, with no public
        # equivalent, so the stubs do not describe it.
        self._startPage()  # type: ignore[attr-defined]

    def save(self) -> None:
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._decorate(total)
            super().showPage()
        super().save()

    def _decorate(self, total: int) -> None:
        width, height = A4
        self.saveState()
        self.setFillColor(_BRAND)
        self.setFont(f"{_FONT}-Bold", 13)
        self.drawString(18 * mm, height - 14 * mm, "JUTSU")
        self.setFillColor(_MUTED)
        self.setFont(_FONT, 8)
        self.drawRightString(width - 18 * mm, height - 14 * mm, REPORT_TITLE)
        self.setStrokeColor(_HAIRLINE)
        self.setLineWidth(0.6)
        self.line(18 * mm, height - 17 * mm, width - 18 * mm, height - 17 * mm)
        self.line(18 * mm, 15 * mm, width - 18 * mm, 15 * mm)
        self.drawString(
            18 * mm,
            10 * mm,
            "Confidential · composed by JUTSU from this handover package's evidence · not stored",
        )
        self.drawRightString(width - 18 * mm, 10 * mm, f"Page {self.getPageNumber()} of {total}")
        self.restoreState()


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle(
            "title", fontName=f"{_FONT}-Bold", fontSize=18, leading=23, textColor=_INK
        ),
        "meta": ParagraphStyle("meta", fontName=_FONT, fontSize=9, leading=13, textColor=_MUTED),
        "h2": ParagraphStyle(
            "h2",
            fontName=f"{_FONT}-Bold",
            fontSize=12,
            leading=16,
            textColor=_BRAND,
            spaceBefore=12,
            spaceAfter=4,
            # A heading never ends a page. Stranded at the foot, with its entries overleaf,
            # it reads as a section with nothing in it; reportlab carries it over instead.
            keepWithNext=1,
        ),
        "body": ParagraphStyle("body", fontName=_FONT, fontSize=9.5, leading=14, textColor=_INK),
        "item": ParagraphStyle(
            "item",
            fontName=f"{_FONT}-Bold",
            fontSize=9.5,
            leading=13,
            textColor=_INK,
            spaceBefore=5,
        ),
        "quote": ParagraphStyle(
            "quote",
            fontName=f"{_FONT}-Italic",
            fontSize=9,
            leading=12.5,
            textColor=_MUTED,
            leftIndent=8,
        ),
        "note": ParagraphStyle("note", fontName=_FONT, fontSize=8.5, leading=12, textColor=_MUTED),
    }


def _date(value: datetime | None) -> str:
    return value.strftime("%d %b %Y") if value is not None else ""


def _narrative(summary: str, clean: _Text) -> list[str]:
    """The model's prose as paragraphs, with the lightest markdown it tends to emit made
    readable — bold markers dropped, list dashes drawn as bullets. Presentation only; no
    word is changed."""
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", summary.strip()):
        lines = []
        for line in block.splitlines():
            stripped = line.strip().replace("**", "").replace("__", "")
            if stripped.startswith(("- ", "* ")):
                stripped = "• " + stripped[2:]
            if stripped:
                lines.append(clean(stripped))
        if lines:
            paragraphs.append("<br/>".join(lines))
    return paragraphs


def render_handover_pdf(report: HandoverReport) -> bytes:
    """The report as a real PDF: embedded fonts, numbered pages, a reference list.

    Pure: no session, no network, no clock beyond what the report carries — so it is tested
    by parsing the bytes back, not by trusting that a library was called.
    """
    clean = _Text(_register_fonts())
    styles = _styles()
    story: list[Any] = []

    story.append(Paragraph(clean(REPORT_TITLE), styles["title"]))
    story.append(Spacer(1, 4))
    who = report.subject_name or "one colleague"
    heading = f"Handover of <b>{clean(who)}</b>"
    if report.subject_role:
        heading += f" ({clean(report.subject_role)})"
    story.append(Paragraph(heading, styles["body"]))
    if report.period_start is not None or report.period_end is not None:
        period = f"Knowledge period: {_date(report.period_start) or 'the beginning'} to {_date(report.period_end) or 'today'}"
    else:
        period = "Knowledge period: the colleague's whole history in JUTSU"
    story.append(Paragraph(clean(period), styles["meta"]))
    story.append(Paragraph(clean(f"Package open until {_date(report.expires_at)}"), styles["meta"]))
    story.append(
        Paragraph(
            clean(f"Generated {report.generated_at.strftime('%d %b %Y, %H:%M')} UTC"),
            styles["meta"],
        )
    )

    story.append(Paragraph("Executive overview", styles["h2"]))
    if report.summary:
        for paragraph in _narrative(report.summary, clean):
            story.append(Paragraph(paragraph, styles["body"]))
            story.append(Spacer(1, 4))
    else:
        story.append(
            Paragraph(
                "The knowledge in this package could not ground an executive summary. JUTSU "
                "generates nothing without evidence; the sections below list what the "
                "package holds.",
                styles["body"],
            )
        )

    def section(block: ReportSection, *, empty: str, note: str | None = None) -> None:
        heading = Paragraph(clean(block.title), styles["h2"])
        if block.state == SECTION_OUT_OF_SCOPE:
            refusal = Paragraph("Not part of this package's scope.", styles["note"])
            story.extend((heading, refusal))
            return
        if not block.items:
            story.extend((heading, Paragraph(empty, styles["note"])))
            return
        # The heading, and a note introducing the entries, ride inside the first entry's
        # group. `keepWithNext` cannot do it here: reportlab never joins a flowable to a
        # KeepTogether (`_ktAllow` refuses containers), so the heading would still end a page.
        lead: list[Any] = [heading]
        if note:
            lead.append(Paragraph(note, styles["note"]))
        for item in block.items:
            line = clean(item.headline)
            if item.date:
                line += f" <font color='#5f6660'>· {clean(item.date)}</font>"
            line += f" <font color='#499f02'>[{item.source}]</font>"
            parts: list[Any] = [*lead, Paragraph(line, styles["item"])]
            lead = []
            if item.quote:
                parts.append(Paragraph(f"“{clean(item.quote)}”", styles["quote"]))
            story.append(KeepTogether(parts))

    for block in report.sections:
        # Non-negotiables 16-18: a list of people is ordered by recency and never scored,
        # and it says so where the people are shown, not only when there are none.
        note = "Listed by most recent mention, never ranked." if block.key == "people" else None
        section(block, empty="No evidence for this section in the package yet.", note=note)
    section(report.documents, empty="No documents in this package's window yet.")

    story.append(Paragraph("Current and open work", styles["h2"]))
    story.append(
        Paragraph(
            "JUTSU does not extract open work as a category of its own. Where the evidence "
            "mentions open work, the executive overview above cites it.",
            styles["note"],
        )
    )

    story.append(Paragraph("Sources", styles["h2"]))
    if report.sources:
        for source in report.sources:
            detail = source.source_system + (f" · {source.date}" if source.date else "")
            story.append(
                Paragraph(
                    f"[{source.number}] {clean(source.title)} <font color='#5f6660'>— {clean(detail)}</font>",
                    styles["body"],
                )
            )
    else:
        story.append(Paragraph("No sources: the package holds no evidence yet.", styles["note"]))

    story.append(Spacer(1, 10))
    story.append(
        Paragraph(
            clean(
                f"Sections list the {report.claims_considered} most recent extracted claims this "
                "summary was grounded on, each with the verbatim passage it was verified "
                "against. Only the executive overview is written by a model, and every "
                "statement in it cites a source above."
            ),
            styles["note"],
        )
    )
    if clean.replaced:
        story.append(
            Paragraph(
                "Some characters in the evidence cannot be drawn by this PDF's font and are "
                "shown as “?”. The KT console shows the original text.",
                styles["note"],
            )
        )

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=24 * mm,
        bottomMargin=22 * mm,
        title=REPORT_TITLE,
        author="JUTSU",
        subject="Knowledge transfer handover summary",
        creator="JUTSU",
    )
    document.build(story, canvasmaker=_NumberedCanvas)
    return buffer.getvalue()
