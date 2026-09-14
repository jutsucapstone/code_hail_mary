"""Extraction's contract, live, against each configured provider. Skipped unless enabled.

The nightly extractor asks for strict JSON and keeps a claim only when its quote appears
verbatim in the passage it names. A provider that cannot do both produces runs with zero
claims, and nothing fails anywhere to say so — so a provider joins the chain after passing
this, not before (ADR 0026).

Synthetic passages only: nothing from a tenant's corpus belongs in a vendor check.

    JUTSU_LIVE_LLM_SMOKE=1 uv run --env-file .env pytest \\
        apps/worker/tests/test_extraction_live_contract.py -q
"""

from __future__ import annotations

import os
import uuid

import pytest
from jutsu_llm import (
    PROVIDER_NAMES,
    FailoverTransport,
    LLMRequest,
    ProviderNotConfigured,
    build_provider,
)
from jutsu_worker import extraction

LIVE_FLAG = "JUTSU_LIVE_LLM_SMOKE"

pytestmark = pytest.mark.skipif(
    os.environ.get(LIVE_FLAG) != "1",
    reason=f"live provider call; set {LIVE_FLAG}=1 to run (spends a small amount)",
)

PASSAGES = (
    "The Atlas project moves the finance ledger from Oracle to PostgreSQL. Priya Raman owns "
    "the cutover, scheduled for 12 October 2026.",
    "On 3 August 2026 the platform team decided to keep the message queue self-hosted rather "
    "than move to a managed service, because of data residency.",
)


@pytest.mark.parametrize("name", PROVIDER_NAMES)
async def test_claims_come_back_as_json_with_verbatim_quotes(name: str) -> None:
    try:
        provider = build_provider(name)
    except ProviderNotConfigured:
        pytest.skip(f"{name} is not configured on this machine")
    transport = FailoverTransport(
        [provider], provider_timeout_s=90.0, total_timeout_s=180.0, max_attempts=1
    )
    chunks = [
        extraction._Chunk(id=uuid.uuid4(), ordinal=index, text=text)
        for index, text in enumerate(PASSAGES)
    ]

    response = await transport.generate(
        LLMRequest(
            system=extraction._SYSTEM,
            prompt=extraction._compose(chunks),
            max_tokens=extraction.EXTRACTION_MAX_TOKENS,
        )
    )
    claims = extraction._parse_claims(response.content)

    assert claims is not None, f"{name} did not return the JSON shape extraction parses"
    verbatim = [
        claim
        for claim in claims
        if isinstance(claim.get("chunk"), int)
        and 1 <= claim["chunk"] <= len(chunks)
        and isinstance(claim.get("quote"), str)
        and claim["quote"]
        and claim["quote"] in chunks[claim["chunk"] - 1].text
    ]
    assert verbatim, f"{name} produced no claim whose quote survives the verbatim gate"
    print(f"\n{name:<11} {len(verbatim)}/{len(claims)} claims verbatim")
