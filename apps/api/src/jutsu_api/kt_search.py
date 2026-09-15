"""What Ask KT reads beside passages: a package's extracted claims (ADR 0028).

Ask KT used to read passages alone, nearest first. A question like "what decisions did A
make?" was answered only when the passage recording a decision happened to sit near the
question in embedding space — while the Decisions tab beside it listed the extracted,
quote-gated decision. This module adds that structured half to the same answer, inside the
same boundary.

**Every claim is read inside the package.** The statement composes `KtScope.conditions`
over the document each claim's evidence chunk belongs to — the `KT_PACKAGE_PREDICATE` the
passages, the tabs and the report compose — plus the claim categories the package covers
and each document's latest finished extraction run. A claim outside the package does not
exist here, and nothing is filtered in Python.

**Every claim cites a real source.** A claim becomes evidence carrying its own chunk and
document, so the citation gate numbers it like a passage, and a citation on it opens the
chunk it was extracted from through the KT evidence door. The model sees the claim's
verbatim quote beside its fields, so it cannot cite a claim without citing that passage.

**Bounded and ranked in SQL.** At most `CLAIM_LIMIT` claims per question: claims of a type
the question asks about first, then claims sharing its words, then the most recent. A
question that names no claim type and shares no claim's words reads no claims — passages
still answer it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from jutsu_retrieval.folders import search_subject_folders
from jutsu_retrieval.search import Evidence
from jutsu_retrieval.terms import query_terms, tsquery_any
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.kt import _LATEST_RUN_JOIN, KtScope

__all__ = [
    "CLAIM_LIMIT",
    "EVIDENCE_CLAIM",
    "EVIDENCE_FOLDER",
    "EVIDENCE_PASSAGE",
    "KtEvidence",
    "claim_intents",
    "claims_for_question",
    "folders_for_question",
    "passage",
    "query_terms",
]

#: The most claims one question reads. A bound on the prompt and on what one answer may
#: stand on; the tabs list everything, this is what one answer needs.
CLAIM_LIMIT: Final = 12

EVIDENCE_PASSAGE: Final = "passage"
EVIDENCE_CLAIM: Final = "claim"
EVIDENCE_FOLDER: Final = "folder"


@dataclass(frozen=True, slots=True)
class KtEvidence:
    """One numbered item an Ask KT answer may cite: a passage, or an extracted claim.

    The shape `synthesise_answer` reads (`Groundable`) plus what the console renders. A
    claim carries its evidence chunk's own id and span — the passage it was extracted
    from — never a span made up for the claim.
    """

    chunk_id: UUID
    document_id: UUID
    document_title: str
    source_system: str
    text: str
    char_start: int
    char_end: int
    score: float
    occurred_at: datetime
    kind: str = EVIDENCE_PASSAGE
    claim_type: str | None = None
    #: Where the source keeps the cited document (ADR 0029) — for a folder, the folder.
    folder_path: str | None = None


def passage(item: Evidence) -> KtEvidence:
    return KtEvidence(
        chunk_id=item.chunk_id,
        document_id=item.document_id,
        document_title=item.document_title,
        source_system=item.source_system,
        text=item.text,
        char_start=item.char_start,
        char_end=item.char_end,
        score=item.score,
        occurred_at=item.occurred_at,
        folder_path=item.folder_path,
    )


#: Words that mean a question is about a claim type. Matched against whole words of the
#: lower-cased question; one question can ask about several types.
_INTENTS: Final[dict[str, frozenset[str]]] = {
    "project": frozenset(
        {"project", "projects", "initiative", "initiatives", "programme", "program", "workstream"}
    ),
    "meeting": frozenset(
        {"meeting", "meetings", "meet", "met", "sync", "syncs", "standup", "standups", "call",
         "calls", "review", "reviews", "retro", "retros"}
    ),
    "person": frozenset(
        {"who", "whom", "contact", "contacts", "people", "person", "colleague", "colleagues",
         "stakeholder", "stakeholders", "team", "teams", "partner", "partners", "worked",
         "collaborated"}
    ),
    "responsibility": frozenset(
        {"responsible", "responsibility", "responsibilities", "own", "owns", "owned", "owner",
         "owners", "ownership", "duty", "duties", "role", "roles", "accountable"}
    ),
    "decision": frozenset(
        {"decision", "decisions", "decide", "decided", "chose", "choose", "chosen", "agreed",
         "agreement", "approved", "approval", "resolved"}
    ),
}  # fmt: skip

_WORD: Final = re.compile(r"[a-z0-9]+")

#: A claim's searchable words: its structured fields and its verbatim quote.
_CLAIM_TEXT: Final = (
    "to_tsvector('english', coalesce(cl.payload_json->>'name', '') || ' ' || "
    "coalesce(cl.payload_json->>'summary', '') || ' ' || coalesce(cl.payload_json->>'quote', ''))"
)


def claim_intents(question: str) -> list[str]:
    """The claim types a question asks about, in taxonomy order."""
    words = set(_WORD.findall(question.lower()))
    return [claim_type for claim_type, cues in _INTENTS.items() if words & cues]


def _claim_evidence(row: Any) -> KtEvidence:
    payload = row.payload_json if isinstance(row.payload_json, dict) else {}
    name = str(payload.get("name") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    quote = str(payload.get("quote") or "").strip()
    date = str(payload.get("date") or "").strip()

    headline = (
        name if not summary or summary == name else (f"{name} — {summary}" if name else summary)
    )
    lines = [f"Extracted {row.claim_type}" + (f": {headline}" if headline else "")]
    if quote:
        lines.append(f'Evidence: "{quote}"')
    if date:
        lines.append(f"Date: {date}")

    return KtEvidence(
        chunk_id=UUID(str(row.chunk_id)),
        document_id=UUID(str(row.document_id)),
        document_title=str(row.document_title),
        source_system=str(row.source_system),
        text="\n".join(lines),
        char_start=int(row.char_start),
        char_end=int(row.char_end),
        score=float(row.confidence),
        occurred_at=row.occurred_at,
        kind=EVIDENCE_CLAIM,
        claim_type=str(row.claim_type),
    )


async def claims_for_question(
    session: AsyncSession, scope: KtScope, question: str, *, limit: int = CLAIM_LIMIT
) -> list[KtEvidence]:
    """The package's claims a question is about, best first, at most `limit`.

    Empty when the package covers no claim category, and when the question names no claim
    type and shares no words with any claim.
    """
    allowed = scope.claim_types()
    intents = [claim_type for claim_type in claim_intents(question) if claim_type in allowed]
    terms = query_terms(question)
    if not allowed or (not intents and not terms):
        return []

    params: dict[str, object] = {
        "allowed_types": allowed,
        "intent_types": intents,
        "limit": max(1, min(limit, CLAIM_LIMIT)),
    }
    filters = [*scope.conditions(params), "cl.claim_type = ANY(:allowed_types)"]
    if terms:
        params["terms"] = tsquery_any(terms)
        lexical = f"ts_rank({_CLAIM_TEXT}, to_tsquery('english', :terms))"
        filters.append(
            f"(cl.claim_type = ANY(:intent_types) OR {_CLAIM_TEXT} @@ to_tsquery('english', :terms))"
        )
    else:
        lexical = "0"
        filters.append("cl.claim_type = ANY(:intent_types)")

    rows = (
        await session.execute(
            text(
                "SELECT cl.claim_type, cl.confidence, cl.payload_json, cl.chunk_id, "  # noqa: S608
                "ch.char_start, ch.char_end, d.id AS document_id, d.title AS document_title, "
                "CAST(s.system AS text) AS source_system, d.created_at AS occurred_at, "
                f"{lexical} AS lexical "
                "FROM extraction_claims cl "
                "JOIN chunks ch ON ch.id = cl.chunk_id "
                "JOIN documents d ON d.id = ch.document_id "
                "JOIN sources s ON s.id = d.source_id "
                + _LATEST_RUN_JOIN
                + f"WHERE {' AND '.join(filters)} "
                "ORDER BY (cl.claim_type = ANY(:intent_types)) DESC, lexical DESC, "
                "COALESCE(NULLIF(cl.payload_json->>'date', ''), "
                "to_char(d.created_at, 'YYYY-MM-DD')) DESC, cl.id DESC "
                "LIMIT :limit"
            ),
            params,
        )
    ).all()
    return [_claim_evidence(row) for row in rows]


async def folders_for_question(
    session: AsyncSession, scope: KtScope, question: str
) -> list[KtEvidence]:
    """The package's folders a question names, each citing a document kept in it (ADR 0029).

    Nothing for a package without `documents`: a folder is where raw documents are kept,
    and such a package has no copilot anyway.
    """
    if "documents" not in scope.categories:
        return []
    folders = await search_subject_folders(
        session,
        subject_user_id=scope.subject_user_id,
        package_id=scope.package_id,
        within=scope.window,
        question=question,
    )
    return [
        KtEvidence(
            chunk_id=folder.chunk_id,
            document_id=folder.document_id,
            document_title=folder.document_title,
            source_system=folder.source_system,
            text=folder.text,
            char_start=folder.char_start,
            char_end=folder.char_end,
            score=folder.score,
            occurred_at=folder.occurred_at,
            kind=EVIDENCE_FOLDER,
            folder_path=folder.folder_path,
        )
        for folder in folders
    ]
