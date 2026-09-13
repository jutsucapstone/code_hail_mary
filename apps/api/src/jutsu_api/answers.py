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

**Model choice lives entirely in `jutsu_llm`** (ADR 0024). The frontend is deliberately
model-agnostic — it renders answers and citations, and the day the model or the vendor
changes nothing in a browser knows. Which provider answered is observability, never part
of the response.

Configuration is honest: a deployment with **no provider configured at all** answers 503
`answers are not configured` before any budget is spent — retrieval keeps working, and
the UI says which half is missing rather than pretending.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from jutsu_llm import INSUFFICIENT_EVIDENCE, any_provider_configured

__all__ = [
    "INSUFFICIENT_EVIDENCE",
    "AnswerOutcome",
    "AnswerTransport",
    "Citation",
    "Groundable",
    "Turn",
    "answers_configured",
    "synthesise_answer",
]


@dataclass(frozen=True, slots=True)
class Turn:
    """One earlier exchange, handed to the model as context and nothing more.

    A KT copilot answers follow-up questions — "and who owned that?" — which the model
    can only resolve if it sees what "that" was. So prior turns go into the prompt. They
    go in as a labelled preamble, **never as numbered passages**: `_grounded` validates
    markers against the evidence list alone, so text in the preamble cannot be cited,
    and an earlier answer cannot launder itself into a source (non-negotiable 3).

    The caller bounds how many and how long; this module trusts nothing about size.
    """

    role: str  # "user" | "assistant"
    content: str


class Groundable(Protocol):
    """What the synthesiser actually reads off a piece of evidence.

    Structural on purpose: retrieval's `Evidence` (a chunk) and a KT insight claim
    both ground an answer, and forcing the claim into the chunk's shape would mean
    inventing char offsets the claim does not have — a fabricated span is worse than
    no span. The gate needs identity and provenance; the prompt needs title and text.
    """

    @property
    def chunk_id(self) -> object: ...
    @property
    def document_id(self) -> object: ...
    @property
    def document_title(self) -> str: ...
    @property
    def source_system(self) -> str: ...
    @property
    def text(self) -> str: ...


#: The token the model is told to emit when the evidence cannot answer. Checked with
#: `in` rather than equality so a polite sentence around it still counts as a refusal.
#:
#: Imported rather than defined here: the provider adapters normalise a vendor's safety
#: refusal to the same string, so there is one spelling of it in the system (ADR 0024).

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
identifiers and never guess what they hide.
6. If a "Conversation so far" section is present, use it ONLY to understand what the \
question refers to. It is not evidence: never cite it, and never repeat a claim from it \
unless a numbered passage supports that claim."""


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
    """Whether this deployment can synthesise answers at all — through **any** provider.

    A chain-wide question rather than one vendor's key: three interchangeable providers
    mean "can we answer" is true whenever any one of them is configured.
    """
    return any_provider_configured()


class AnswerTransport(Protocol):
    """One model call, behind a seam.

    The same pattern as the embedding client and the OAuth transport: the real
    implementation talks to a paid provider, so every test injects a fake — and the
    grounding gate is tested against deliberately misbehaving fakes, which no live
    model can be asked to be on demand.

    **The implementation lives in `jutsu_llm`** (ADR 0024): a chain of three vendors,
    tried in order, behind exactly this method. There is no vendor-specific transport
    class anywhere above it — a second one would be a second error mapping, a second
    model lookup and a second refusal convention, drifting from the chain's the first
    time either was touched.

    Nothing else about this module moved. The prompt, the passage numbering, the marker
    gate, the single retry and the refusal are here, above the transport, exactly where
    they were — which is what makes which vendor answered indistinguishable to
    everything downstream.
    """

    async def complete(self, *, system: str, prompt: str) -> str: ...


def _compose_prompt(
    question: str, evidence: Sequence[Groundable], history: Sequence[Turn] = ()
) -> str:
    passages = "\n\n".join(
        f"[{index}] {item.document_title} ({item.source_system})\n{item.text}"
        for index, item in enumerate(evidence, start=1)
    )
    preamble = ""
    if history:
        # Labelled, un-numbered, and placed before the passages: the gate only ever
        # resolves a marker against the numbered list, so nothing here is citable.
        turns = "\n".join(
            f"{'Recipient' if turn.role == 'user' else 'JUTSU'}: {turn.content}" for turn in history
        )
        preamble = (
            "Conversation so far (context only — this is not evidence and must never be "
            f"cited):\n\n{turns}\n\n"
        )
    return f"{preamble}Evidence passages:\n\n{passages}\n\nQuestion: {question}"


def _grounded(text: str, evidence: Sequence[Groundable]) -> tuple[str, list[Citation]] | None:
    """The hallucination gate. Returns None unless every citation checks out.

    Three refusals: an explicit INSUFFICIENT_EVIDENCE, a marker naming a passage that
    was never retrieved, and an answer with no markers at all. The last one matters
    most — it is the fluent, plausible, uncited paragraph that non-negotiable 3 exists
    to keep out of the product.
    """
    cleaned = text.strip()
    if not cleaned or INSUFFICIENT_EVIDENCE in cleaned:
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
    transport: AnswerTransport,
    *,
    question: str,
    evidence: Sequence[Groundable],
    history: Sequence[Turn] = (),
) -> AnswerOutcome:
    """A grounded answer, or an honest refusal. Never a fluent guess.

    With no evidence there is nothing to ground on, so the refusal is immediate and
    free — the model is not asked to confirm that nothing is nothing. That holds with a
    conversation behind the question too: history is context for reading the question,
    not something an answer may stand on.
    """
    if not evidence:
        return AnswerOutcome(answer=None, citations=[], insufficient_evidence=True, attempts=0)

    prompt = _compose_prompt(question, evidence, history)

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
