"""What an employee's own documents say, as extracted claims (ADR 0030).

Cited Q&A used to read passages alone. A question like "which decisions did I make?" was
answered only when the passage recording a decision happened to sit near the question in
embedding space — while extraction had already written that decision down, quote-gated, beside
its evidence chunk. This module reads those claims for the asker, inside the asker's own
boundary.

**The caller's own ACL, in SQL, with no way to widen it.** Like `search_chunks` and
`search_folders`, this takes a `user_id` and resolves the principals itself, in the caller's
transaction; the tenant comes from the session GUC. There is no principals parameter, no org
parameter, no subject and no package — a knowledge-transfer package answers a different
question about a different person and has its own implementation (ADR 0025, ADR 0028).

**Two bounded arms, both keyed on equality.**

- *Anchored*: claims extracted from the passages the vector search just returned. Those chunk
  ids are already the caller's own authorized set, and the claim is re-authorized here anyway.
- *Intent*: when the question names a kind of claim — "decisions", "who", "responsible" — the
  most recent `INTENT_WINDOW` claims of that kind among the caller's documents.

Both are `= ANY(...)` over indexed columns, which is what row-level security permits as an
index condition: `uuid` and `text` equality are leakproof, so `ix_extraction_claims_chunk_id`
and `ix_extraction_claims_org_type` serve the application role. Everything else — the question's
words as a full-text rank, the claim's recency — orders the bounded pool rather than selecting
it.

**Current claims only, and the latest run is found once.** A claim counts when its run is the
latest FINISHED extraction run for its document, which is what makes a re-extraction supersede
rather than accumulate (non-negotiable 4). That lookup is computed for the candidate documents
alone: keyed on `stats_json->>'document_id'`, whose operator is not leakproof, so a correlated
per-claim subquery cannot use its index beneath a policy — measured at 27 s for 5,000 documents
as the application role, against 129 ms for this shape at 50,000 (ADR 0030).

**Nothing here is a fact until an answer cites it.** A claim becomes evidence carrying its own
chunk and document, so the citation gate numbers it like a passage and a citation on it opens
the passage it was extracted from, through the door that passage already has.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from jutsu_db.acl import resolve_acl_principals
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_retrieval.links import safe_source_uri
from jutsu_retrieval.search import ACL_PREDICATE, ORG_SCOPE_SQL
from jutsu_retrieval.terms import query_terms, tsquery_any

__all__ = [
    "CLAIMS_STATEMENT",
    "CLAIM_INTENTS",
    "CLAIM_LIMIT",
    "INTENT_WINDOW",
    "MAX_ANCHORS",
    "ClaimEvidence",
    "claim_intents",
    "search_claims",
]

#: The most claims one question reads. A bound on the prompt and on what one answer may stand
#: on: the knowledge surfaces list everything, this is what one answer needs.
CLAIM_LIMIT: Final = 12

#: How many of a kind's most recent claims one question ranks. A bound on the work a question
#: can cause, and the one place this search trades recall for it: a claim that is neither
#: extracted from a retrieved passage nor among this many of its kind is not read by this arm.
INTENT_WINDOW: Final = 200

#: The most retrieved passages whose claims are read. `DEFAULT_K` is 30 and `/v1/ask` allows
#: 100; beyond this the anchored arm stops growing rather than the statement.
MAX_ANCHORS: Final = 60

#: Words that mean a question is about a kind of claim, matched against whole words of the
#: lower-cased question. One question can ask about several kinds.
#:
#: The same vocabulary Ask KT uses, deliberately duplicated rather than shared: KT keeps its own
#: dedicated implementation (ADR 0030), and `test_cited_qa_scope` pins the two lists equal so
#: neither can drift silently.
CLAIM_INTENTS: Final[dict[str, frozenset[str]]] = {
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

#: NULL when a question carries no words of its own, and `to_tsquery` is strict, so the rank is
#: NULL and the `COALESCE` makes it zero. No second statement, and no empty-tsquery notice.
_RANK: Final = f"COALESCE(ts_rank({_CLAIM_TEXT}, to_tsquery('english', CAST(:terms AS text))), 0)"

_ANCHORED: Final = "cl.chunk_id = ANY(CAST(:anchors AS uuid[]))"
_OF_A_KIND: Final = "cl.claim_type = ANY(CAST(:intent_types AS text[]))"

#: The tenant twice — the claim's and its document's — the supersession filter, and the caller's
#: own ACL. Composed into every arm of the statement, so no candidate is ever selected by a
#: looser rule than the one the projection applies (S608: `ORG_SCOPE_SQL` and `ACL_PREDICATE`
#: are module constants; everything a caller supplies is a bound parameter).
_AUTHORIZED: Final = (
    f"cl.org_id = {ORG_SCOPE_SQL} AND d.org_id = {ORG_SCOPE_SQL} "
    f"AND d.superseded_by IS NULL AND {ACL_PREDICATE}"
)

_CLAIM_JOINS: Final = (
    "JOIN chunks ch ON ch.id = cl.chunk_id AND ch.org_id = cl.org_id "
    "JOIN documents d ON d.id = ch.document_id AND d.org_id = ch.org_id "
)

#: The candidates, authorized before anything else runs: claims from the passages just
#: retrieved, and the most recent claims of the kinds the question asks about.
_POOL: Final = (
    "pool AS MATERIALIZED ("  # noqa: S608
    f"(SELECT cl.id, ch.document_id FROM extraction_claims cl {_CLAIM_JOINS}"
    f"WHERE {_AUTHORIZED} AND {_ANCHORED}) "
    "UNION "
    f"(SELECT cl.id, ch.document_id FROM extraction_claims cl {_CLAIM_JOINS}"
    f"WHERE {_AUTHORIZED} AND {_OF_A_KIND} "
    "ORDER BY d.created_at DESC, cl.id DESC LIMIT :window))"
)

#: The latest finished run per candidate document, computed once. Restricted to the pool's own
#: documents: the key is a JSON expression, so this is a scan whatever is written, and scanning
#: it once for the handful of candidate documents is what keeps the cost flat.
_LATEST: Final = (
    "latest AS MATERIALIZED ("  # noqa: S608
    "SELECT DISTINCT ON (r.stats_json->>'document_id') r.id, "
    "r.stats_json->>'document_id' AS document_id "
    f"FROM extraction_runs r WHERE r.org_id = {ORG_SCOPE_SQL} AND r.finished_at IS NOT NULL "
    "AND r.stats_json->>'document_id' IN (SELECT CAST(p.document_id AS text) FROM pool p) "
    "ORDER BY r.stats_json->>'document_id', r.started_at DESC)"
)

#: One statement, so the escalation-free path a test reads is the path production runs. Ordered:
#: the kinds asked about first, then the question's words, then claims from retrieved passages,
#: then what the claim itself dates, then the most recent document.
CLAIMS_STATEMENT: Final = (
    f"WITH {_POOL}, {_LATEST} "  # noqa: S608
    "SELECT cl.claim_type, cl.confidence, cl.payload_json, cl.chunk_id, "
    "ch.char_start, ch.char_end, d.id AS document_id, d.title AS document_title, "
    "CAST(s.system AS text) AS source_system, d.created_at AS occurred_at, "
    f"d.folder_path, d.uri AS source_uri, {_RANK} AS lexical "
    "FROM pool p "
    "JOIN extraction_claims cl ON cl.id = p.id "
    "JOIN latest l ON l.id = cl.run_id AND l.document_id = CAST(p.document_id AS text) "
    + _CLAIM_JOINS
    + "JOIN sources s ON s.id = d.source_id "
    f"WHERE {_AUTHORIZED} "
    f"ORDER BY ({_OF_A_KIND}) DESC, lexical DESC, ({_ANCHORED}) DESC, "
    "COALESCE(NULLIF(cl.payload_json->>'date', ''), to_char(d.created_at, 'YYYY-MM-DD')) DESC, "
    "cl.id DESC LIMIT :limit"
)


@dataclass(frozen=True, slots=True)
class ClaimEvidence:
    """One extracted claim, as a numbered item an answer can cite.

    The shape `synthesise_answer` reads (`Groundable`) plus what a citation renders. The chunk
    id and span are the passage the claim was extracted from — never a span invented for the
    claim, because a fabricated offset is worse than none.
    """

    chunk_id: UUID
    document_id: UUID
    document_title: str
    source_system: str
    text: str
    char_start: int
    char_end: int
    #: The extractor's confidence, which is what ranks a claim against another claim. It is not
    #: a cosine similarity and is never compared with one.
    score: float
    occurred_at: datetime
    claim_type: str
    folder_path: str | None = None
    source_uri: str | None = None


def claim_intents(question: str) -> list[str]:
    """The kinds of claim a question asks about, in taxonomy order."""
    words = set(_WORD.findall(question.lower()))
    return [claim_type for claim_type, cues in CLAIM_INTENTS.items() if words & cues]


def _evidence(row: Any) -> ClaimEvidence:
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

    return ClaimEvidence(
        chunk_id=UUID(str(row.chunk_id)),
        document_id=UUID(str(row.document_id)),
        document_title=str(row.document_title),
        source_system=str(row.source_system),
        text="\n".join(lines),
        char_start=int(row.char_start),
        char_end=int(row.char_end),
        score=float(row.confidence),
        occurred_at=row.occurred_at,
        claim_type=str(row.claim_type),
        folder_path=row.folder_path,
        source_uri=safe_source_uri(row.source_uri),
    )


async def search_claims(
    session: AsyncSession,
    *,
    user_id: UUID,
    question: str,
    anchor_chunk_ids: Sequence[UUID] = (),
    limit: int = CLAIM_LIMIT,
) -> list[ClaimEvidence]:
    """The caller's own current claims a question is about, best first, at most `limit`.

    Empty when the question names no kind of claim and no passage was retrieved to anchor one —
    passages answer such a question as they always did.

    `anchor_chunk_ids` are chunks the caller has just been authorized to read by the vector
    search. They narrow; they cannot widen. Every claim returned is re-authorized here by the
    same predicate, so an id from anywhere else selects nothing.
    """
    intents = claim_intents(question)
    anchors = [str(chunk_id) for chunk_id in dict.fromkeys(anchor_chunk_ids)][:MAX_ANCHORS]
    if not intents and not anchors:
        return []

    principals, groups = await resolve_acl_principals(session, user_id=user_id)
    terms = query_terms(question)
    rows = (
        await session.execute(
            text(CLAIMS_STATEMENT),
            {
                "principals": sorted(principals),
                "groups": sorted(groups),
                "anchors": anchors,
                "intent_types": intents,
                "terms": tsquery_any(terms) or None,
                "window": INTENT_WINDOW,
                "limit": max(1, min(limit, CLAIM_LIMIT)),
            },
        )
    ).all()
    return [_evidence(row) for row in rows]
