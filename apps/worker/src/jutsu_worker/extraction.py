"""LLM extraction: masked chunks in, evidence-anchored claims out. Spec §10, first slice.

The non-negotiables this module exists to satisfy, and where each one lives:

1. **Every derived claim carries evidence** — `extraction_claims.chunk_id` is NOT NULL
   and the payload records the verbatim quote with its offsets *within that chunk's
   masked text*. (Offsets into the original body would require replaying mask spans;
   the chunk id + masked-text offsets identify the span exactly and re-derivably.)
2. **The hallucination gate** — a claim whose `quote` does not appear verbatim in the
   chunk it names is DISCARDED, and the discard is counted in the run's stats. The
   model is instructed to quote; the gate is what makes the instruction true.
3. **Versioned, superseding, never overwriting** — every execution writes a new
   `extraction_runs` row and new claims under it. Nothing deletes or edits old claims;
   the read model selects the latest finished run per document. Entity-level
   `superseded_by` linking is entity resolution's job (spec §11) and is deliberately
   not faked here with document-level pointers.

**The model reads masked text only** — the same text an authorized caller may already
read, minus every span the PII pass covered. Masking tokens are named as opaque in the
prompt so the model does not guess at them.

**Model choice**: `JUTSU_EXTRACTION_MODEL`, default `claude-opus-5`, through the
official SDK. The spec's §5 stack names Gemini for LLM work; extraction runs on the
Claude API by the product owner's explicit direction ("I will provide Claude API") —
recorded here because a silent stack substitution is the §22.9 failure mode.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CLAIM_TYPES",
    "EXTRACTOR_VERSION",
    "AnthropicExtractionTransport",
    "ExtractionResult",
    "ExtractionTransport",
    "extract_document",
    "extraction_configured",
    "extraction_job_key",
    "extraction_model",
]

logger = logging.getLogger("jutsu.worker.extraction")

EXTRACTOR_VERSION = "1.0.0"

CLAIM_TYPES = ("decision", "person", "project", "meeting", "responsibility")

#: One call's context ceiling, in characters of masked chunk text. Documents longer
#: than this are extracted over their first window and the run's stats say how much was
#: covered — a partial honest extraction beats a silently truncated prompt.
MAX_WINDOW_CHARS = 24_000

_SYSTEM = """You are JUTSU's knowledge extractor. You read numbered passages from ONE \
workplace document and emit structured claims about them as strict JSON.

Emit ONLY a JSON object of this exact shape, no prose, no code fences:
{"claims": [{"type": "...", "chunk": 1, "quote": "...", "summary": "...", \
"name": "...", "date": "YYYY-MM-DD", "confidence": 0.0}]}

Rules, in order of importance:
1. "quote" MUST be copied verbatim, character for character, from the passage named by
   "chunk" (1-based). A claim whose quote is not verbatim will be discarded.
2. "type" is one of: decision, person, project, meeting, responsibility.
   - decision: something was decided or chosen. summary states the decision.
   - person: a named individual acting in the document. name holds the person's name.
   - project: a named project, system or initiative. name holds it.
   - meeting: a meeting, call or discussion that occurred. summary describes it;
     date if the passage states one.
   - responsibility: someone owns or is accountable for something. summary states it;
     name holds the owner if named.
3. "confidence" is your honest 0..1 estimate. Do not inflate it.
4. Include "date" only when the passage itself states it. Never infer dates.
5. Passages contain masking tokens like [EMAIL_A7]; treat them as opaque identifiers,
   never guess what they hide, and never use one as a person's name.
6. Fewer, well-evidenced claims beat many speculative ones. If a passage supports no
   claim, emit none for it."""


def extraction_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def extraction_model() -> str:
    return os.environ.get("JUTSU_EXTRACTION_MODEL", "").strip() or "claude-opus-5"


def extraction_job_key(org_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """One extraction per document VERSION: superseding a document inserts a new row
    with a new id, so a changed document re-extracts under a new key while a re-run of
    the same version deduplicates."""
    return f"extract.document:{org_id}:{document_id}"


class ExtractionTransport(Protocol):
    """One model call, behind the same seam every paid provider sits behind here."""

    async def complete(self, *, system: str, prompt: str) -> str: ...


class AnthropicExtractionTransport:
    async def complete(self, *, system: str, prompt: str) -> str:
        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model=extraction_model(),
            max_tokens=8192,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            return '{"claims": []}'
        return "".join(block.text for block in response.content if block.type == "text")


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    run_id: uuid.UUID
    stored: int
    gated: int
    chunks_covered: int
    chunks_total: int


@dataclass(frozen=True, slots=True)
class _Chunk:
    id: uuid.UUID
    ordinal: int
    text: str


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse_claims(raw: str) -> list[dict[str, Any]] | None:
    """The model's JSON, or None when it is not JSON at all.

    Code fences are stripped as transcription noise; anything deeper than that is a
    parse failure the caller retries once and then records — never guesses around.
    """
    cleaned = _FENCE.sub("", raw.strip()).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    claims = payload.get("claims") if isinstance(payload, dict) else None
    if not isinstance(claims, list):
        return None
    return [claim for claim in claims if isinstance(claim, dict)]


def _window(chunks: list[_Chunk]) -> list[_Chunk]:
    window: list[_Chunk] = []
    used = 0
    for chunk in chunks:
        if used + len(chunk.text) > MAX_WINDOW_CHARS and window:
            break
        window.append(chunk)
        used += len(chunk.text)
    return window


def _compose(chunks: list[_Chunk]) -> str:
    passages = "\n\n".join(
        f"[{index}]\n{chunk.text}" for index, chunk in enumerate(chunks, start=1)
    )
    return f"Document passages:\n\n{passages}\n\nEmit the claims JSON now."


async def extract_document(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    transport: ExtractionTransport,
) -> ExtractionResult:
    """Extract one document's claims into a new, versioned run.

    Completes even when the model produces nothing usable — a run with zero claims and
    `parse_failed` in its stats is an observable outcome, where a dead-lettered job
    would block this document's slot in the queue forever over a transient model mood.
    """
    rows = (
        await session.execute(
            text("SELECT id, ordinal, text FROM chunks WHERE document_id = :doc ORDER BY ordinal"),
            {"doc": document_id},
        )
    ).all()
    chunks = [_Chunk(id=row.id, ordinal=row.ordinal, text=row.text) for row in rows]
    window = _window(chunks)

    prompt_hash = hashlib.sha256((_SYSTEM + "|window-v1").encode("utf-8")).hexdigest()
    run_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO extraction_runs (id, org_id, extractor_version, prompt_hash, model) "
            "VALUES (:id, :org, :version, :hash, :model)"
        ),
        {
            "id": run_id,
            "org": str(org_id),
            "version": EXTRACTOR_VERSION,
            "hash": prompt_hash,
            "model": extraction_model(),
        },
    )

    stored = 0
    gated = 0
    parse_failed = False

    if window:
        prompt = _compose(window)
        raw = await transport.complete(system=_SYSTEM, prompt=prompt)
        claims = _parse_claims(raw)
        if claims is None:
            retry = (
                f"{prompt}\n\nYour previous output was not valid JSON of the required "
                "shape. Emit ONLY the JSON object, nothing else."
            )
            raw = await transport.complete(system=_SYSTEM, prompt=retry)
            claims = _parse_claims(raw)
        if claims is None:
            parse_failed = True
            claims = []

        for claim in claims:
            claim_type = claim.get("type")
            chunk_index = claim.get("chunk")
            quote = claim.get("quote")
            if claim_type not in CLAIM_TYPES:
                gated += 1
                continue
            if not isinstance(chunk_index, int) or not 1 <= chunk_index <= len(window):
                gated += 1
                continue
            if not isinstance(quote, str) or not quote:
                gated += 1
                continue
            chunk = window[chunk_index - 1]
            # THE GATE (non-negotiable 2): verbatim, or it never existed.
            position = chunk.text.find(quote)
            if position == -1:
                gated += 1
                continue

            confidence = claim.get("confidence")
            if not isinstance(confidence, int | float) or not 0 <= confidence <= 1:
                confidence = 0.5

            payload = {
                "summary": str(claim.get("summary") or "")[:1000],
                "name": str(claim.get("name") or "")[:255] or None,
                "date": str(claim.get("date") or "")[:10] or None,
                "quote": quote[:2000],
                "char_start": position,
                "char_end": position + len(quote),
                "document_id": str(document_id),
                "extractor_version": EXTRACTOR_VERSION,
                "prompt_hash": prompt_hash,
                "model": extraction_model(),
            }
            await session.execute(
                text(
                    "INSERT INTO extraction_claims (id, run_id, chunk_id, org_id, "
                    "claim_type, payload_json, confidence) "
                    "VALUES (gen_random_uuid(), :run, :chunk, :org, :type, "
                    "cast(:payload AS jsonb), :confidence)"
                ),
                {
                    "run": run_id,
                    "chunk": chunk.id,
                    "org": str(org_id),
                    "type": claim_type,
                    "payload": json.dumps(payload),
                    "confidence": float(confidence),
                },
            )
            stored += 1

    stats = {
        "document_id": str(document_id),
        "claims_stored": stored,
        "claims_gated": gated,
        "chunks_covered": len(window),
        "chunks_total": len(chunks),
        "parse_failed": parse_failed,
    }
    await session.execute(
        text(
            "UPDATE extraction_runs SET finished_at = now(), "
            "stats_json = cast(:stats AS jsonb) WHERE id = :id"
        ),
        {"stats": json.dumps(stats), "id": run_id},
    )
    logger.info(
        "extraction_run org=%s document=%s stored=%d gated=%d covered=%d/%d",
        org_id,
        document_id,
        stored,
        gated,
        len(window),
        len(chunks),
    )
    return ExtractionResult(
        run_id=run_id,
        stored=stored,
        gated=gated,
        chunks_covered=len(window),
        chunks_total=len(chunks),
    )
