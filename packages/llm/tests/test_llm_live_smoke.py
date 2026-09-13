"""One live call per configured provider. Skipped unless explicitly enabled.

**This is the test mocks cannot replace.** Everything in `test_llm_failover.py` proves a
property of *our* code against a scripted transport, which is the right way to test
ordering, budgets and error mapping. None of it proves that a key works, that a model id
still exists, that the endpoint still answers the shape the adapter parses, or that the
vendor has not renamed a field — and a fallback chain whose fallbacks have never been
called is a chain nobody has tested.

**It costs money**, a few dozen tokens per configured provider, so it is gated behind
`JUTSU_LIVE_LLM_SMOKE=1` and CI never runs it:

    JUTSU_LIVE_LLM_SMOKE=1 uv run --env-file .env pytest \\
        packages/llm/tests/test_llm_live_smoke.py -q -s

A provider with no key is skipped rather than failed: "not configured" is a deployment
fact, and a smoke test that failed on it would be unrunnable on every machine that holds
one key.

It asks a question with no retrieved evidence and no company data in it — the point is
that the vendor answers at all, and nothing about JUTSU's corpus needs to leave the
building to establish that.
"""

from __future__ import annotations

import os

import pytest
from jutsu_llm import (
    DEFAULT_ORDER,
    FailoverTransport,
    LLMRequest,
    ProviderNotConfigured,
    build_chain,
    build_provider,
    configured_order,
)

LIVE_FLAG = "JUTSU_LIVE_LLM_SMOKE"

pytestmark = pytest.mark.skipif(
    os.environ.get(LIVE_FLAG) != "1",
    reason=f"live provider call; set {LIVE_FLAG}=1 to run (spends a small amount)",
)

#: Deliberately trivial and deliberately not about JUTSU. Nothing from a tenant's corpus
#: belongs in a connectivity check.
_SYSTEM = "You are a terse assistant. Answer in one short sentence."
_PROMPT = "Reply with the single word: ready"


@pytest.mark.parametrize("name", DEFAULT_ORDER)
async def test_each_configured_provider_answers(name: str) -> None:
    """Credential, endpoint, model id and response shape — all four at once.

    Prints provider, model and latency so a run is a readable report rather than a row of
    dots. It never prints a key, and never prints the answer: a live model's output is not
    interesting here and printing it would set the wrong precedent for a file that will
    one day be pointed at a real prompt.
    """
    try:
        provider = build_provider(name)
    except ProviderNotConfigured:
        pytest.skip(f"{name} is not configured on this machine")

    response = await provider.generate(
        LLMRequest(system=_SYSTEM, prompt=_PROMPT, max_tokens=32), timeout_s=30.0
    )

    assert response.content.strip()
    assert response.provider == name
    assert response.model == provider.model
    print(f"\n{name:<11} model={response.model:<28} {response.latency_ms:>6} ms")


async def test_the_configured_chain_answers_end_to_end() -> None:
    """The whole chain, as `/v1/ask` would use it — minus retrieval, prompt and gate.

    Proves the thing the parametrised test above cannot: that *some* provider in this
    deployment's configured order actually answers, which is the claim the availability
    layer exists to make.
    """
    providers = build_chain()
    if not providers:
        pytest.skip("no provider is configured on this machine")

    transport = FailoverTransport(providers)
    answer = await transport.generate(LLMRequest(system=_SYSTEM, prompt=_PROMPT, max_tokens=32))

    assert answer.content.strip()
    print(
        f"\nchain {list(configured_order())} answered by "
        f"{answer.provider} ({answer.model}) in {answer.latency_ms} ms"
    )
