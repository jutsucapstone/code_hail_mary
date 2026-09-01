"""Answer synthesis: a grounded answer over retrieved evidence, or a refusal.

Non-negotiable 3, implemented: **answers are assembled from retrieved evidence, never
model memory. Uncited assertions → retry once → insufficient_evidence.** The pipeline is

    retrieve (ACL-filtered, unchanged)  →  compose with numbered evidence
    →  model answers citing [n]         →  validate every citation against the
                                           retrieved set  →  retry once  →  refuse

The validation is the load-bearing half. The model is *instructed* to cite, but an
instruction is not a gate: `_grounded()` parses the answer's markers, refuses any that
name evidence outside the retrieved set, and refuses an answer with no citations at
all. A fluent uncited paragraph is a defect here, not a near-miss.

**The model reads masked text and never the original bodies.** Same rule as search:
what leaves the tenant boundary is what an authorized caller may already read, minus
every span the PII pass covered.

**Model choice lives on the server** (`JUTSU_ANSWER_MODEL`, default `claude-opus-5`).
The frontend is deliberately model-agnostic — it renders answers and citations, and the
day the model changes nothing in a browser knows.

Configuration is honest: no `ANTHROPIC_API_KEY` means `POST /v1/ask` answers 503
`answers are not configured` before any budget is spent — retrieval keeps working, and
the UI says which half is missing rather than pretending.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Protocol

import anthropic
from jutsu_core.errors import ServiceUnavailable
from jutsu_retrieval.search import Evidence

__all__ = [
    "AnswerOutcome",
    "AnswerTransport",
    "AnthropicTransport",
    "Citation",
    "answers_configured",
    "synthesise_answer",
]

_DEFAULT_MODEL = "claude-opus-5"

#: The token the model is told to emit when the evidence cannot answer. Checked with
#: `in` rather than equality so a polite sentence around it still counts as a refusal.
_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

_MARKER = re.compile(r"\[(\d{1,3})\]")

_SYSTEM = """You are JUTSU, an enterprise memory assistant. Answer the user's question \
using ONLY the numbered evidence passages provided. Rules, in order of importance:

1. Every factual claim in your answer MUST carry a citation marker like [1] or [2] \
naming the passage it came from. A sentence without a citation will be discarded.
2. If the evidence does not contain enough to answer, respond with exactly \
INSUFFICIENT_EVIDENCE and nothing else. Never answer from general knowledge.
3. Cite only passage numbers that exist. Do not invent passages.
4. Be concise: a short, direct answer with citations beats a long summary.
5. The passages may contain masking tokens like [EMAIL_A7]; treat them as opaque \
identifiers and never guess what they hide."""


@dataclass(frozen=True, slots=True)
class Citation:
    marker: int
    chunk_id: str
    document_id: str
    document_title: str
    source_system: str


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    #: None when the evidence could not answer — the UI renders the refusal state, not
    #: an empty string pretending to be an answer.
    answer: str | None
    citations: list[Citation]
    insufficient_evidence: bool
    #: How many model calls it took (1 or 2). Surfaced for cost accounting, §20.
    attempts: int


def answers_configured() -> bool:
    """Whether this deployment can synthesise answers at all."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def answer_model() -> str:
    return os.environ.get("JUTSU_ANSWER_MODEL", "").strip() or _DEFAULT_MODEL


class AnswerTransport(Protocol):
    """One model call, behind a seam.

    The same pattern as the embedding client and the OAuth transport: the real
    implementation talks to a paid provider, so every test injects a fake — and the
    grounding gate is tested against deliberately misbehaving fakes, which no live
    model can be asked to be on demand.
    """

    async def complete(self, *, system: str, prompt: str) -> str: ...


class AnthropicTransport:
    """The real call, through the official SDK.

    Thinking is left at the model's default (adaptive on this model family); the answer
    format is controlled by the system prompt and validated by `_grounded`, not trusted.
    """

    async def complete(self, *, system: str, prompt: str) -> str:
        client = anthropic.AsyncAnthropic()
        try:
            response = await client.messages.create(
                model=answer_model(),
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.RateLimitError as exc:
            raise ServiceUnavailable(
                "The answer service is briefly over capacity. Try again shortly."
            ) from exc
        except anthropic.APIStatusError as exc:
            # The provider's message can carry request details; classify, never forward.
            raise ServiceUnavailable("The answer service did not respond.") from exc
        except anthropic.APIConnectionError as exc:
            raise ServiceUnavailable("The answer service is unreachable.") from exc

        if response.stop_reason == "refusal":
            # The safety layer declined. Not an evidence problem, but the honest
            # rendering is the same: no answer, no invented text.
            return _INSUFFICIENT

        return "".join(block.text for block in response.content if block.type == "text")


def _compose_prompt(question: str, evidence: list[Evidence]) -> str:
    passages = "\n\n".join(
        f"[{index}] {item.document_title} ({item.source_system})\n{item.text}"
        for index, item in enumerate(evidence, start=1)
    )
    return f"Evidence passages:\n\n{passages}\n\nQuestion: {question}"


def _grounded(text: str, evidence: list[Evidence]) -> tuple[str, list[Citation]] | None:
    """The hallucination gate. Returns None unless every citation checks out.

    Three refusals: an explicit INSUFFICIENT_EVIDENCE, a marker naming a passage that
    was never retrieved, and an answer with no markers at all. The last one matters
    most — it is the fluent, plausible, uncited paragraph that non-negotiable 3 exists
    to keep out of the product.
    """
    cleaned = text.strip()
    if not cleaned or _INSUFFICIENT in cleaned:
        return None

    markers = [int(m) for m in _MARKER.findall(cleaned)]
    if not markers:
        return None
    valid = set(range(1, len(evidence) + 1))
    if any(marker not in valid for marker in markers):
        return None

    seen: list[int] = []
    for marker in markers:
        if marker not in seen:
            seen.append(marker)
    citations = [
        Citation(
            marker=marker,
            chunk_id=str(evidence[marker - 1].chunk_id),
            document_id=str(evidence[marker - 1].document_id),
            document_title=evidence[marker - 1].document_title,
            source_system=evidence[marker - 1].source_system,
        )
        for marker in seen
    ]
    return cleaned, citations


async def synthesise_answer(
    transport: AnswerTransport, *, question: str, evidence: list[Evidence]
) -> AnswerOutcome:
    """A grounded answer, or an honest refusal. Never a fluent guess.

    With no evidence there is nothing to ground on, so the refusal is immediate and
    free — the model is not asked to confirm that nothing is nothing.
    """
    if not evidence:
        return AnswerOutcome(answer=None, citations=[], insufficient_evidence=True, attempts=0)

    prompt = _compose_prompt(question, evidence)

    first = await transport.complete(system=_SYSTEM, prompt=prompt)
    grounded = _grounded(first, evidence)
    if grounded is not None:
        answer, citations = grounded
        return AnswerOutcome(
            answer=answer, citations=citations, insufficient_evidence=False, attempts=1
        )

    # Retry once, with the failure named. One retry, not a loop: a model that cannot
    # ground twice is telling us the evidence does not support an answer.
    retry_prompt = (
        f"{prompt}\n\nYour previous answer was rejected because it was not properly "
        "grounded: every claim must cite an existing passage number like [1], and if "
        "the evidence cannot answer, reply exactly INSUFFICIENT_EVIDENCE."
    )
    second = await transport.complete(system=_SYSTEM, prompt=retry_prompt)
    grounded = _grounded(second, evidence)
    if grounded is not None:
        answer, citations = grounded
        return AnswerOutcome(
            answer=answer, citations=citations, insufficient_evidence=False, attempts=2
        )

    return AnswerOutcome(answer=None, citations=[], insufficient_evidence=True, attempts=2)
