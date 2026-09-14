"""The citation gate, live, against each configured provider. Skipped unless enabled.

`test_llm_live_smoke.py` proves a vendor answers. This proves what a fallback is for: that
its answer survives JUTSU's own gate. The gate is a formatting contract — `[n]` markers
against numbered passages, or `INSUFFICIENT_EVIDENCE` — and a provider whose answers the
gate discards is a fallback that never falls back. A provider joins the chain after passing
this, not before (ADR 0026).

Synthetic passages only: nothing from a tenant's corpus belongs in a vendor check.

    JUTSU_LIVE_LLM_SMOKE=1 uv run --env-file .env pytest \\
        apps/api/tests/test_answers_live_contract.py -q
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest
from jutsu_api.answers import synthesise_answer
from jutsu_llm import PROVIDER_NAMES, FailoverTransport, ProviderNotConfigured, build_provider

LIVE_FLAG = "JUTSU_LIVE_LLM_SMOKE"

pytestmark = pytest.mark.skipif(
    os.environ.get(LIVE_FLAG) != "1",
    reason=f"live provider call; set {LIVE_FLAG}=1 to run (spends a small amount)",
)


@dataclass(frozen=True, slots=True)
class Passage:
    chunk_id: str
    document_id: str
    document_title: str
    source_system: str
    text: str


EVIDENCE = (
    Passage(
        "c1",
        "d1",
        "Ledger migration plan",
        "local",
        "The Atlas project moves the finance ledger from Oracle to PostgreSQL. Priya Raman "
        "owns the cutover, scheduled for 12 October 2026.",
    ),
    Passage(
        "c2",
        "d2",
        "Queue decision notes",
        "local",
        "On 3 August 2026 the platform team decided to keep the message queue self-hosted "
        "rather than move to a managed service, because of data residency.",
    ),
)


def alone(name: str) -> FailoverTransport:
    """One provider and one attempt: a fallback must never be what makes this pass."""
    try:
        provider = build_provider(name)
    except ProviderNotConfigured:
        pytest.skip(f"{name} is not configured on this machine")
    return FailoverTransport(
        [provider], provider_timeout_s=60.0, total_timeout_s=150.0, max_attempts=1
    )


@pytest.mark.parametrize("name", PROVIDER_NAMES)
async def test_an_answer_the_passages_support_comes_back_cited(name: str) -> None:
    outcome = await synthesise_answer(
        alone(name),
        question="Who owns the Atlas cutover, and what was decided about the message queue?",
        evidence=EVIDENCE,
    )

    assert outcome.insufficient_evidence is False, f"{name}'s answer did not survive the gate"
    assert outcome.citations
    assert {citation.chunk_id for citation in outcome.citations} <= {"c1", "c2"}
    print(f"\n{name:<11} grounded in {outcome.attempts} attempt(s)")


@pytest.mark.parametrize("name", PROVIDER_NAMES)
async def test_a_question_the_passages_cannot_answer_is_refused(name: str) -> None:
    outcome = await synthesise_answer(
        alone(name), question="What is the company's parental leave policy?", evidence=EVIDENCE
    )

    assert outcome.insufficient_evidence is True, f"{name} answered past its evidence"
    assert outcome.answer is None
    print(f"\n{name:<11} refused in {outcome.attempts} attempt(s)")
