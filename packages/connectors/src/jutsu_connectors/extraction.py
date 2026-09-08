"""Turning an uploaded file into text, or saying honestly that it cannot be.

The ingestion pipeline consumes `RawDocument.body`, which is a `str`. So a file is
**searchable** only if there is a real path from its bytes to text, and **stored** —
kept, listed, downloadable — when there is not. That distinction is the product's, not an
implementation detail: an employee who uploads a scanned PDF or a recording is told it is
stored and not searchable, rather than watching it sit in "processing" for ever.

**Every reader here parses untrusted input**, so each is bounded before it is called and
none of them is asked to be clever. A parser that runs out of memory takes the worker
with it, and a worker that dies mid-job leaves a lease to expire — which is recoverable
but slow, and entirely avoidable by refusing early.

**Nothing here touches the network or the filesystem.** Bytes in, text out, so the whole
module is testable without a bucket, and an extractor can never be the thing that reaches
somewhere it should not (§13's SSRF concern applies to parsers too — several document
formats can reference external entities).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "MAX_EXTRACT_BYTES",
    "ExtractedText",
    "Extraction",
    "UnsupportedContent",
    "extract",
    "plan_for",
]


class UnsupportedContent(Exception):
    """The bytes are not what they claimed, or the format cannot yield text safely.

    Distinct from "this format is stored only": that is a plan, decided before any bytes
    are read. This is a failure discovered while reading them.
    """


#: The largest object an extractor is handed. Well past a real document and far short of
#: what would put a worker under memory pressure — the upload ceiling is 512 MiB, and
#: handing a parser half a gigabyte because the *upload* allowed it is how a bound
#: becomes decorative.
MAX_EXTRACT_BYTES: Final = 32 * 1024 * 1024

#: Ceilings inside a container format. Each is a place a malicious file tries to spend
#: unbounded time or memory, and each is cheap to refuse.
_MAX_PDF_PAGES: Final = 2_000
_MAX_DOCX_PARAGRAPHS: Final = 50_000
_MAX_PPTX_SLIDES: Final = 1_000
_MAX_SHEET_ROWS: Final = 20_000
_MAX_CSV_ROWS: Final = 50_000

#: How much text is kept. A document past this is truncated rather than refused — the
#: first megabyte of a book is still worth searching, and the alternative is telling
#: somebody their file "failed".
MAX_TEXT_CHARS: Final = 1_000_000


@dataclass(frozen=True, slots=True)
class ExtractedText:
    text: str
    #: True when the document was longer than `MAX_TEXT_CHARS`. Surfaced so the UI can
    #: say so rather than letting a silent truncation look like a complete index.
    truncated: bool


@dataclass(frozen=True, slots=True)
class Extraction:
    """What will be done with a file, decided from its type before any bytes are read."""

    #: `text` — extract and index. `store` — keep it, do not pretend to read it.
    mode: str
    #: Why, in one sentence, for the interface. Only set for `store`.
    reason: str | None = None


#: Formats whose text this can genuinely reach, keyed by the MIME the bytes resolve to.
#:
#: The value is the reader's name; `extract` dispatches on it. Kept as data so the
#: supported list and the dispatch cannot disagree — a format present here with no reader
#: raises at import in the test below rather than at runtime for a customer.
_TEXT_FORMATS: Final[dict[str, str]] = {
    "text/plain": "plain",
    "text/markdown": "plain",
    "text/csv": "csv",
    "text/tab-separated-values": "csv",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel.sheet.macroEnabled.12": "xlsx",
}

#: Formats that are kept and deliberately not read, with the reason a person gets told.
#:
#: These are honest limits of the deployed stack, not oversights — saying so in the
#: interface is the whole point of this table existing.
_STORE_ONLY: Final[dict[str, str]] = {
    "image/png": "Images are stored but not searched — there is no text recognition yet.",
    "image/jpeg": "Images are stored but not searched — there is no text recognition yet.",
    "image/gif": "Images are stored but not searched — there is no text recognition yet.",
    "image/webp": "Images are stored but not searched — there is no text recognition yet.",
    "video/mp4": "Video is stored but not searched — there is no transcription yet.",
    "video/webm": "Video is stored but not searched — there is no transcription yet.",
    "video/quicktime": "Video is stored but not searched — there is no transcription yet.",
    "video/x-msvideo": "Video is stored but not searched — there is no transcription yet.",
    "audio/mpeg": "Audio is stored but not searched — there is no transcription yet.",
    "audio/wav": "Audio is stored but not searched — there is no transcription yet.",
    "audio/ogg": "Audio is stored but not searched — there is no transcription yet.",
    "audio/mp4": "Audio is stored but not searched — there is no transcription yet.",
    "application/zip": (
        "Archives are stored but not opened. Upload the files inside to make them searchable."
    ),
    "application/x-ole-storage": (
        "This is an older Office format (.doc, .xls, .ppt). It is stored, but only the "
        "newer .docx, .xlsx and .pptx formats can be read."
    ),
}


def plan_for(mime: str) -> Extraction | None:
    """What to do with this type, or None when it is not accepted at all.

    Three answers, and the third is the one that keeps the product honest: extract it,
    store it and say why it is not searchable, or refuse it outright.
    """
    if mime in _TEXT_FORMATS:
        return Extraction(mode="text")
    if mime in _STORE_ONLY:
        return Extraction(mode="store", reason=_STORE_ONLY[mime])
    return None


#: Control characters that survive a decode and mean nothing in a document body. NUL in
#: particular cannot be stored in a Postgres text column at all, so this is correctness
#: rather than tidiness.
_CONTROLS: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANK_LINES: Final = re.compile(r"\n{3,}")


def _tidy(parts: list[str]) -> ExtractedText:
    joined = "\n".join(part for part in parts if part).strip()
    joined = _CONTROLS.sub(" ", joined)
    joined = _BLANK_LINES.sub("\n\n", joined)
    if len(joined) > MAX_TEXT_CHARS:
        return ExtractedText(text=joined[:MAX_TEXT_CHARS], truncated=True)
    return ExtractedText(text=joined, truncated=False)


def _decode(data: bytes) -> str:
    """Text from bytes, without ever raising on an encoding.

    A file that is 99% valid UTF-8 with one bad byte is still worth indexing, and
    refusing it would mean telling somebody their notes "failed" over a stray character.

    **UTF-16 is tried only behind a byte-order mark, and that ordering is the whole
    subtlety.** UTF-16 decodes almost any even-length byte string without raising — it
    just produces nonsense — so putting it in a plain fallback chain means a Latin-1 file
    with one accented character silently becomes a page of CJK mojibake instead of
    falling through to cp1252. It has to be selected by evidence, not tried by position.
    """
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _plain(data: bytes) -> list[str]:
    return [_decode(data)]


def _csv_text(data: bytes) -> list[str]:
    """Rows flattened to lines, so a spreadsheet reads as prose to a chunker."""
    rows: list[str] = []
    reader = csv.reader(io.StringIO(_decode(data)))
    try:
        for index, record in enumerate(reader):
            if index >= _MAX_CSV_ROWS:
                break
            line = " ".join(cell.strip() for cell in record if cell and cell.strip())
            if line:
                rows.append(line)
    except csv.Error as exc:
        # A field past `csv.field_size_limit()` raises mid-iteration. Whatever was read
        # before it is still worth keeping.
        if not rows:
            raise UnsupportedContent("That file could not be read as text.") from exc
    return rows


def _pdf(data: bytes) -> list[str]:
    # No `from pypdf.errors import PdfError`: that name does not exist in pypdf 6, and
    # pinning this module to one release's exception hierarchy would break on an upgrade
    # for no benefit — everything below is turned into one refusal anyway.
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        # An encrypted PDF is a real thing to be sent and cannot be read without the
        # password. Saying so beats a stack trace.
        if reader.is_encrypted:
            raise UnsupportedContent(
                "That PDF is password-protected, so its text could not be read."
            )
        pages = reader.pages[:_MAX_PDF_PAGES]
        return [page.extract_text() or "" for page in pages]
    except UnsupportedContent:
        raise
    except Exception as exc:
        raise UnsupportedContent("That PDF could not be read.") from exc


def _docx(data: bytes) -> list[str]:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise UnsupportedContent("That Word document could not be read.") from exc

    parts = [p.text for p in document.paragraphs[:_MAX_DOCX_PARAGRAPHS] if p.text.strip()]
    # Tables carry a great deal of what people actually write down, and skipping them
    # silently loses the half of a document that is most worth searching.
    for table in document.tables:
        for row in table.rows:
            line = " ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
            if line:
                parts.append(line)
    return parts


def _pptx(data: bytes) -> list[str]:
    from pptx import Presentation

    try:
        deck = Presentation(io.BytesIO(data))
    except Exception as exc:
        raise UnsupportedContent("That presentation could not be read.") from exc

    parts: list[str] = []
    for index, slide in enumerate(deck.slides):
        if index >= _MAX_PPTX_SLIDES:
            break
        for shape in slide.shapes:
            text = getattr(shape, "text", "")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        # Speaker notes are where the argument usually lives.
        notes = getattr(slide, "notes_slide", None)
        frame = getattr(notes, "notes_text_frame", None) if notes is not None else None
        if frame is not None and getattr(frame, "text", "").strip():
            parts.append(frame.text.strip())
    return parts


def _xlsx(data: bytes) -> list[str]:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise UnsupportedContent("That spreadsheet could not be read.") from exc

    parts: list[str] = []
    try:
        for sheet in workbook.worksheets:
            parts.append(f"# {sheet.title}")
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= _MAX_SHEET_ROWS:
                    break
                line = " ".join(str(c).strip() for c in row if c is not None and str(c).strip())
                if line:
                    parts.append(line)
    except Exception as exc:
        # `read_only=True` parses lazily, so corrupt sheet XML raises HERE rather than at
        # open — the same trap the bulk-onboarding reader hit.
        if len(parts) <= 1:
            raise UnsupportedContent("That spreadsheet could not be read.") from exc
    finally:
        workbook.close()
    return parts


_READERS: Final = {
    "plain": _plain,
    "csv": _csv_text,
    "pdf": _pdf,
    "docx": _docx,
    "pptx": _pptx,
    "xlsx": _xlsx,
}


def extract(data: bytes, *, mime: str) -> ExtractedText:
    """Text from a file whose plan is `text`.

    Raises `UnsupportedContent` when the bytes turn out not to be readable — which is a
    per-file failure the employee can see and act on, never a worker crash.
    """
    if len(data) > MAX_EXTRACT_BYTES:
        raise UnsupportedContent(
            "That file is too large to read. It is stored, but not searchable."
        )
    reader_name = _TEXT_FORMATS.get(mime)
    if reader_name is None:
        raise UnsupportedContent("That file type cannot be read as text.")
    return _tidy(_READERS[reader_name](data))
