"""The provider chain: what it tries, in what order, and what it must never do (ADR 0024).

Three kinds of test, and the middle one is the one that would catch a real regression.

**The chain**, against scripted providers: order, fallback on each retryable class, the
attempt ceiling, the total budget, and the single most important property — that every
provider is handed the *identical* request, because a fallback answering a subtly
different question is worse than no fallback at all.

**The adapters**, against `httpx.MockTransport`. These run the real mapping code — 429,
503, 408, a malformed 200, an empty completion, a safety refusal — rather than a fake that
agrees with it. A hand-written stand-in for a status-code branch proves that the stand-in
is correct.

**The boundary**: no key in a log line, no key in an exception, no provider key anywhere a
browser could reach, and no session, tenant or principal reachable from this layer at all.

**Nothing here imports an application.** `jutsu_llm` is a package: `apps/api` and
`apps/worker` both depend on it and it may depend on neither, so the tests that prove the
citation gate still holds over a fallback answer live in `apps/api/tests/test_ask.py`
where `synthesise_answer` does.

Nothing here makes a paid call. `test_llm_live_smoke.py` is the opt-in that does.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from jutsu_core.errors import ServiceUnavailable
from jutsu_llm import (
    DEFAULT_CEREBRAS_MODEL,
    DEFAULT_GROQ_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_ORDER,
    INSUFFICIENT_EVIDENCE,
    AllProvidersFailed,
    CerebrasProvider,
    FailoverTransport,
    GroqProvider,
    LLMRequest,
    LLMResponse,
    OpenRouterProvider,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderRefused,
    ProviderTimeout,
    ProviderUnavailable,
    any_provider_configured,
    build_chain,
    configured_order,
    provider_status,
    split_list,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Every environment variable that can hold a provider credential. Used by the leak tests,
#: and listed once so a fourth provider cannot be added without this list noticing.
KEY_ENVS = ("CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY")

SECRET = "sk-test-DO-NOT-LOG-9f3a2b"


@pytest.fixture(autouse=True)
def _no_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """No provider configuration leaks in from the developer's own `.env`.

    The root conftest loads `.env`, so without this a machine with real keys in it runs a
    different suite from CI — `build_chain()` would return providers the test never asked
    for, and the config tests would pass or fail by accident of whose laptop ran them.
    """
    for name in (*KEY_ENVS, "LLM_PROVIDER_ORDER", "OPENROUTER_FALLBACK_MODELS"):
        monkeypatch.delenv(name, raising=False)
    for name in ("CEREBRAS_MODEL", "GROQ_MODEL", "OPENROUTER_MODEL"):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------------ scripted providers


class FakeProvider:
    """A provider that answers, raises, or takes too long — and records what it was asked."""

    def __init__(
        self,
        name: str,
        *,
        answer: str | None = None,
        error: Exception | None = None,
        delay_s: float = 0.0,
        model: str = "fake-model",
    ) -> None:
        self.name = name
        self.model = model
        self._answer = answer
        self._error = error
        self._delay_s = delay_s
        self.requests: list[LLMRequest] = []
        self.timeouts: list[float] = []

    async def generate(self, request: LLMRequest, *, timeout_s: float) -> LLMResponse:
        self.requests.append(request)
        self.timeouts.append(timeout_s)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._error is not None:
            raise self._error
        return LLMResponse(
            content=self._answer or f"answered by {self.name} [1]",
            provider=self.name,
            model=self.model,
            latency_ms=1,
        )


def chain(*providers: FakeProvider, **kwargs: Any) -> FailoverTransport:
    kwargs.setdefault("provider_timeout_s", 5.0)
    kwargs.setdefault("total_timeout_s", 30.0)
    kwargs.setdefault("max_attempts", 4)
    return FailoverTransport(list(providers), **kwargs)


async def ask(transport: FailoverTransport) -> str:
    return await transport.complete(system="SYSTEM", prompt="PROMPT")


class TestTheChain:
    async def test_the_primary_answers_and_nothing_else_is_called(self) -> None:
        # The overwhelmingly common case, and the one a fallback layer must not make
        # slower or more expensive: Cerebras answers, nobody else is asked or paid.
        cerebras = FakeProvider("cerebras")
        openrouter = FakeProvider("openrouter")

        assert "cerebras" in await ask(chain(cerebras, openrouter))

        assert len(cerebras.requests) == 1
        assert openrouter.requests == []

    @pytest.mark.parametrize(
        "error",
        [
            ProviderTimeout("cerebras"),
            ProviderRateLimited("cerebras"),
            ProviderUnavailable("cerebras", "status 503"),
            ProviderRefused("cerebras", "status 400"),
        ],
        ids=["timeout", "rate_limited", "unavailable", "refused"],
    )
    async def test_every_provider_failure_class_falls_over(self, error: Exception) -> None:
        # Timeout, 429 and 5xx are the obvious ones. `refused` is the deliberate decision:
        # the provider is never retried, but the *chain* continues, because one vendor's
        # stricter validation or one unrotated key must not take down a request the other
        # two would have answered.
        cerebras = FakeProvider("cerebras", error=error)
        openrouter = FakeProvider("openrouter")

        assert "openrouter" in await ask(chain(cerebras, openrouter))

    async def test_it_walks_the_whole_chain_until_something_answers(self) -> None:
        cerebras = FakeProvider("cerebras", error=ProviderTimeout("cerebras"))
        openrouter = FakeProvider("openrouter", error=ProviderRateLimited("openrouter"))
        groq = FakeProvider("groq")

        assert "groq" in await ask(chain(cerebras, openrouter, groq))

        assert len(groq.requests) == 1

    async def test_when_every_provider_fails_the_caller_sees_the_existing_error(self) -> None:
        # `ServiceUnavailable` is what `/v1/ask` has always raised and what the error
        # envelope renders as a 503 — no schema change, no new failure mode to handle.
        transport = chain(
            FakeProvider("cerebras", error=ProviderUnavailable("cerebras")),
            FakeProvider("groq", error=ProviderUnavailable("groq")),
        )

        with pytest.raises(ServiceUnavailable):
            await ask(transport)

    async def test_exhaustion_carries_the_last_error_class_for_the_worker(self) -> None:
        """`AllProvidersFailed` is a `ServiceUnavailable` that says what happened.

        The API wants the 503 and nothing else. The worker classifies a failed extraction
        job into retryable and non-retryable kinds, and "every vendor was rate limited"
        and "every vendor refused the request" call for opposite answers — so the label
        rides on the exception rather than in the message a caller can read.
        """
        transport = chain(
            FakeProvider("cerebras", error=ProviderRateLimited("cerebras")),
            FakeProvider("groq", error=ProviderRefused("groq", "status 400")),
        )

        with pytest.raises(AllProvidersFailed) as caught:
            await ask(transport)

        assert caught.value.error_class == "refused"
        assert isinstance(caught.value, ServiceUnavailable)
        # Never in the envelope: `details` is rendered to the caller verbatim.
        assert caught.value.details == {}

    async def test_the_error_never_names_a_provider(self) -> None:
        # §11. A caller must not learn which vendors JUTSU uses, let alone which was
        # unwell, from an error message.
        transport = chain(
            FakeProvider("cerebras", error=ProviderRateLimited("cerebras")),
            FakeProvider("groq", error=ProviderRateLimited("groq")),
        )

        with pytest.raises(ServiceUnavailable) as caught:
            await ask(transport)

        message = str(caught.value)
        for name in DEFAULT_ORDER:
            assert name not in message.lower()

    async def test_an_empty_chain_refuses_rather_than_crashing(self) -> None:
        with pytest.raises(AllProvidersFailed) as caught:
            await ask(chain())

        assert caught.value.error_class == "not_configured"

    async def test_the_attempt_ceiling_is_honoured(self) -> None:
        # §27: one user request, a hard maximum number of paid attempts. With the ceiling
        # at two, the third provider is never asked even though it would have answered.
        third = FakeProvider("groq")
        transport = chain(
            FakeProvider("cerebras", error=ProviderTimeout("cerebras")),
            FakeProvider("openrouter", error=ProviderTimeout("openrouter")),
            third,
            max_attempts=2,
        )

        with pytest.raises(ServiceUnavailable):
            await ask(transport)

        assert third.requests == []


class TestFailureInjection:
    """§25, stated as sequences: who was actually called, in order, in each outage shape.

    The tests above prove each hop. These prove the *paths* — including the one that
    matters commercially, where nothing is wrong and only the primary is paid.
    """

    @staticmethod
    def _providers(*errors: Exception | None) -> list[FakeProvider]:
        names = ["cerebras", "openrouter", "groq"]
        return [FakeProvider(name, error=error) for name, error in zip(names, errors, strict=True)]

    @pytest.mark.parametrize(
        ("errors", "expected_called", "answered_by"),
        [
            pytest.param(
                (None, None, None),
                ["cerebras"],
                "cerebras",
                id="primary-answers-nobody-else-is-paid",
            ),
            pytest.param(
                (ProviderTimeout("cerebras"), None, None),
                ["cerebras", "openrouter"],
                "openrouter",
                id="cerebras-timeout-to-openrouter",
            ),
            pytest.param(
                (ProviderRateLimited("cerebras"), ProviderRateLimited("openrouter"), None),
                ["cerebras", "openrouter", "groq"],
                "groq",
                id="two-rate-limited-to-groq",
            ),
            pytest.param(
                (
                    ProviderRefused("cerebras", "status 400"),
                    ProviderUnavailable("openrouter", "status 503"),
                    None,
                ),
                ["cerebras", "openrouter", "groq"],
                "groq",
                id="the-outage-that-actually-happened",
            ),
        ],
    )
    async def test_the_chain_takes_the_expected_path(
        self,
        errors: tuple[Exception | None, ...],
        expected_called: list[str],
        answered_by: str,
    ) -> None:
        providers = self._providers(*errors)

        answer = await ask(chain(*providers))

        called = [provider.name for provider in providers if provider.requests]
        assert called == expected_called
        assert answered_by in answer

    async def test_a_vendor_refusing_everything_no_longer_stops_the_system(self) -> None:
        """The production failure this whole layer exists to answer.

        A single vendor answering 400 to every request, for five days, with nothing to
        fall over to — every `/v1/ask` a 503 and every nightly extraction job
        `provider_permanent`. With a chain, the same refusal costs one wasted attempt per
        request and the answer arrives from the next vendor.
        """
        broken = FakeProvider("cerebras", error=ProviderRefused("cerebras", "status 400"))
        healthy = FakeProvider("openrouter")

        answer = await ask(chain(broken, healthy))

        assert "openrouter" in answer
        assert len(broken.requests) == 1, "refused once, never retried"

    async def test_the_request_is_identical_across_the_chain(self) -> None:
        # §16. This is the property that makes a fallback answer *the same answer to the
        # same question*. A chain that trimmed the prompt, appended an error, or
        # re-retrieved between attempts would still pass every other test in this file.
        cerebras = FakeProvider("cerebras", error=ProviderTimeout("cerebras"))
        openrouter = FakeProvider("openrouter", error=ProviderTimeout("openrouter"))
        groq = FakeProvider("groq")
        transport = chain(cerebras, openrouter, groq)

        await ask(transport)

        seen = [cerebras.requests[0], openrouter.requests[0], groq.requests[0]]
        assert all(request == seen[0] for request in seen)
        assert seen[0].system == "SYSTEM"
        assert seen[0].prompt == "PROMPT"

    async def test_the_request_cannot_be_mutated_between_attempts(self) -> None:
        # Frozen by construction, so "the same request" is enforced rather than promised.
        request = LLMRequest(system="s", prompt="p")

        with pytest.raises(dataclasses.FrozenInstanceError):
            request.prompt = "edited"  # type: ignore[misc]

    async def test_a_callers_own_token_ceiling_reaches_every_provider(self) -> None:
        # Extraction asks for 8192 where the answer path asks for 4096, and it must not
        # matter which vendor serves it: a fallback that silently truncated to its own
        # default would return half a document's claims and look like a short document.
        cerebras = FakeProvider("cerebras", error=ProviderTimeout("cerebras"))
        groq = FakeProvider("groq")
        transport = chain(cerebras, groq)

        await transport.generate(LLMRequest(system="s", prompt="p", max_tokens=8192))

        assert [request.max_tokens for request in cerebras.requests] == [8192]
        assert [request.max_tokens for request in groq.requests] == [8192]

    async def test_generate_returns_the_provider_that_actually_answered(self) -> None:
        # What extraction writes into every claim's provenance. A response that named the
        # configured primary rather than the vendor that served it would put a false
        # attribution on evidence, which is the one thing this codebase may not do.
        transport = chain(
            FakeProvider("cerebras", error=ProviderUnavailable("cerebras")),
            FakeProvider("groq", model="openai/gpt-oss-120b"),
        )

        response = await transport.generate(LLMRequest(system="s", prompt="p"))

        assert response.provider == "groq"
        assert response.model == "openai/gpt-oss-120b"

    def test_the_transport_cannot_reach_a_tenant_a_user_or_a_database(self) -> None:
        # Asserted structurally rather than by outcome. The chain takes two strings.
        # There is no session, no principal and no org id to widen, which is what keeps
        # ACL filtering upstream where it belongs — the same shape as
        # `search_chunks(…)` taking no `org_id`.
        parameters = set(inspect.signature(FailoverTransport.complete).parameters)

        assert parameters == {"self", "system", "prompt"}
        for forbidden in ("session", "user_id", "org_id", "principal", "db"):
            assert forbidden not in parameters


class TestTheBudget:
    async def test_a_provider_never_gets_more_than_its_slice(self) -> None:
        cerebras = FakeProvider("cerebras")
        await ask(chain(cerebras, provider_timeout_s=7.0, total_timeout_s=30.0))

        assert cerebras.timeouts == [7.0]

    async def test_a_provider_never_gets_more_than_what_remains(self) -> None:
        # The per-provider timeout is a ceiling, not an entitlement: with two seconds of
        # total budget left, a thirty-second provider timeout would let one vendor
        # overrun the request's own deadline.
        #
        # `approx`, because the slice is the remaining budget — two seconds minus however
        # long it took to get here. Windows' `time.monotonic()` has ~16 ms granularity and
        # returned exactly 2.0, so an exact comparison passed locally; Linux's nanosecond
        # clock returned 1.99999903 and failed in CI. What the test is about is that the
        # provider got two seconds rather than thirty.
        cerebras = FakeProvider("cerebras")
        await ask(chain(cerebras, provider_timeout_s=30.0, total_timeout_s=2.0))

        assert cerebras.timeouts == pytest.approx([2.0], abs=0.05)

    async def test_the_chain_stops_when_the_budget_is_gone(self) -> None:
        # A slow first provider must not be able to spend the whole request and then have
        # two more vendors tried anyway. The total is the outer bound.
        slow = FakeProvider("cerebras", error=ProviderTimeout("cerebras"), delay_s=0.3)
        never = FakeProvider("groq")
        transport = chain(slow, never, provider_timeout_s=0.2, total_timeout_s=0.25)

        with pytest.raises(ServiceUnavailable):
            await ask(transport)

        assert never.requests == []


class TestConfiguration:
    def test_the_default_order_is_cerebras_openrouter_groq(self) -> None:
        assert configured_order() == ("cerebras", "openrouter", "groq")

    def test_the_order_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Ordering is deployment policy, not a constant somebody has to redeploy to change.
        monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq, cerebras")

        assert configured_order() == ("groq", "cerebras")

    def test_the_order_may_be_separated_by_semicolons(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `gcloud run deploy --set-env-vars` splits its own argument on commas, so a
        # comma-separated list cannot be set that way without rewriting the whole flag
        # into gcloud's alternate-delimiter form. The second spelling costs nothing and
        # keeps the deployment command boring.
        monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq;openrouter")

        assert configured_order() == ("groq", "openrouter")
        assert split_list("a; b,c ") == ["a", "b", "c"]

    def test_an_unknown_name_is_dropped_rather_than_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq,gemini,cerebras")

        assert configured_order() == ("groq", "cerebras")

    def test_an_entirely_unknown_order_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A typo in one variable must not leave a deployment with no providers at all.
        monkeypatch.setenv("LLM_PROVIDER_ORDER", "gemini,llama")

        assert configured_order() == DEFAULT_ORDER

    def test_an_unconfigured_provider_is_left_out_of_the_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Cerebras and Groq configured, OpenRouter absent: the chain is the two that
        # exist, in order, and nothing crashes over the one that does not.
        monkeypatch.setenv("CEREBRAS_API_KEY", SECRET)
        monkeypatch.setenv("GROQ_API_KEY", SECRET)

        assert [provider.name for provider in build_chain()] == ["cerebras", "groq"]

    def test_one_provider_configured_is_a_chain_of_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # What makes adding or removing a vendor a configuration change rather than a
        # code change: one key configured is one attempt and the same error sentences.
        monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)

        assert [provider.name for provider in build_chain()] == ["openrouter"]

    def test_no_provider_configured_is_reported_honestly(self) -> None:
        assert build_chain() == []
        assert any_provider_configured() is False

    @pytest.mark.parametrize("key", KEY_ENVS)
    def test_any_one_vendor_is_enough_to_answer(
        self, key: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The gate is chain-wide. A gate reading one vendor's key would refuse every
        # question on a deployment where a different, working provider sat configured.
        monkeypatch.setenv(key, SECRET)

        assert any_provider_configured() is True

    def test_a_provider_without_a_key_is_not_configured(self) -> None:
        # Constructed rather than requested: an unconfigured provider is absent from the
        # chain instead of consuming one of its attempts on the request path.
        for provider in (CerebrasProvider, OpenRouterProvider, GroqProvider):
            with pytest.raises(ProviderNotConfigured):
                provider()

    def test_the_status_view_names_models_and_never_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GROQ_API_KEY", SECRET)
        monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")

        rows = {row["provider"]: row for row in provider_status()}

        assert rows["groq"]["state"] == "configured"
        assert rows["groq"]["model"] == "openai/gpt-oss-120b"
        assert rows["cerebras"]["state"] == "not_configured"
        assert rows["cerebras"]["model"] == ""
        assert SECRET not in json.dumps(rows)


# ------------------------------------------------------------------ the real adapters


def mock_transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def ok_body(content: str = "an answer [1]", finish: str = "stop") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


class TestTheOpenAICompatibleAdapter:
    """Cerebras, Groq and OpenRouter share this code, so it is tested once, for real."""

    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GROQ_API_KEY", SECRET)

    async def test_a_successful_call_is_normalised(self) -> None:
        # Whatever the vendor's envelope looks like, what leaves the adapter is one
        # shape, so nothing downstream — and no browser — needs provider-specific code.
        provider = GroqProvider(
            transport=mock_transport(lambda r: httpx.Response(200, json=ok_body()))
        )

        response = await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

        assert response.content == "an answer [1]"
        assert response.provider == "groq"
        assert response.input_tokens == 11
        assert response.output_tokens == 7

    async def test_the_system_prompt_travels_as_a_system_message(self) -> None:
        # §28: the adapter converts the shape, never the content. The exact strings JUTSU
        # composed arrive as the system and user messages, unedited.
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=ok_body())

        provider = GroqProvider(transport=mock_transport(handler))
        await provider.generate(LLMRequest(system="SYSTEM", prompt="PROMPT"), timeout_s=5)

        assert seen["messages"] == [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "PROMPT"},
        ]

    async def test_the_callers_token_ceiling_is_what_is_sent(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=ok_body())

        provider = GroqProvider(transport=mock_transport(handler))
        await provider.generate(LLMRequest(system="s", prompt="p", max_tokens=8192), timeout_s=5)

        assert seen["max_tokens"] == 8192

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (429, ProviderRateLimited),
            (408, ProviderTimeout),
            (500, ProviderUnavailable),
            (502, ProviderUnavailable),
            (503, ProviderUnavailable),
            (504, ProviderUnavailable),
            (400, ProviderRefused),
            (401, ProviderRefused),
            (403, ProviderRefused),
            (404, ProviderRefused),
        ],
    )
    async def test_status_codes_map_to_the_taxonomy(
        self, status: int, expected: type[Exception]
    ) -> None:
        provider = GroqProvider(
            transport=mock_transport(lambda r: httpx.Response(status, json={"error": "x"}))
        )

        with pytest.raises(expected):
            await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

    async def test_a_timeout_is_a_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        provider = GroqProvider(transport=mock_transport(handler))

        with pytest.raises(ProviderTimeout):
            await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

    async def test_a_connection_failure_is_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host", request=request)

        provider = GroqProvider(transport=mock_transport(handler))

        with pytest.raises(ProviderUnavailable):
            await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

    async def test_a_malformed_200_is_a_provider_fault(self) -> None:
        # A 200 that is not the documented shape is a malfunction, not an answer. Letting
        # it through as empty text would reach the citation gate and be rendered as "the
        # evidence does not support this".
        provider = GroqProvider(
            transport=mock_transport(lambda r: httpx.Response(200, json={"nonsense": True}))
        )

        with pytest.raises(ProviderUnavailable):
            await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

    async def test_an_empty_completion_is_a_provider_fault(self) -> None:
        provider = GroqProvider(
            transport=mock_transport(lambda r: httpx.Response(200, json=ok_body(content="   ")))
        )

        with pytest.raises(ProviderUnavailable):
            await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

    @pytest.mark.parametrize("reason", ["content_filter", "refusal", "safety"])
    async def test_a_safety_refusal_becomes_the_existing_refusal_sentinel(
        self, reason: str
    ) -> None:
        # Not a provider failure and not an answer: exactly what JUTSU already renders
        # when a vendor's safety layer declines. Normalised here so `_grounded` upstream
        # keeps recognising it whichever vendor produced it.
        provider = GroqProvider(
            transport=mock_transport(
                lambda r: httpx.Response(200, json=ok_body(content="", finish=reason))
            )
        )

        response = await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

        assert response.content == INSUFFICIENT_EVIDENCE

    async def test_the_key_is_sent_as_a_bearer_token_and_nowhere_else(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = request.content.decode()
            seen["url"] = str(request.url)
            return httpx.Response(200, json=ok_body())

        provider = GroqProvider(transport=mock_transport(handler))
        await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

        assert seen["auth"] == f"Bearer {SECRET}"
        assert SECRET not in seen["body"]
        # §4.9's other half: never in a URL, where it would reach access logs and proxies.
        assert SECRET not in seen["url"]


class TestEachVendorIsWiredToItsOwnEndpoint:
    """One shared adapter, three vendors — so the per-vendor wiring is what can drift.

    Asserted per provider rather than once, because the shared code being right says
    nothing about whether Cerebras reads `CEREBRAS_API_KEY` or posts to Cerebras.
    """

    @pytest.mark.parametrize(
        ("factory", "key_env", "name", "host"),
        [
            (CerebrasProvider, "CEREBRAS_API_KEY", "cerebras", "api.cerebras.ai"),
            (OpenRouterProvider, "OPENROUTER_API_KEY", "openrouter", "openrouter.ai"),
            (GroqProvider, "GROQ_API_KEY", "groq", "api.groq.com"),
        ],
    )
    async def test_each_provider_authenticates_and_posts_to_its_own_vendor(
        self,
        factory: Any,
        key_env: str,
        name: str,
        host: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(key_env, SECRET)
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=ok_body())

        provider = factory(transport=mock_transport(handler))
        response = await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

        assert response.provider == name
        assert host in seen["url"]
        assert seen["auth"] == f"Bearer {SECRET}"

    @pytest.mark.parametrize(
        ("factory", "key_env", "other_env"),
        [
            (CerebrasProvider, "CEREBRAS_API_KEY", "GROQ_API_KEY"),
            (OpenRouterProvider, "OPENROUTER_API_KEY", "CEREBRAS_API_KEY"),
            (GroqProvider, "GROQ_API_KEY", "OPENROUTER_API_KEY"),
        ],
    )
    def test_a_provider_never_borrows_another_vendors_key(
        self, factory: Any, key_env: str, other_env: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A shared base class makes this exactly the mistake that would be invisible: with
        # one vendor's key set, a provider reading the wrong variable would look
        # configured and authenticate against the wrong account.
        monkeypatch.setenv(other_env, SECRET)

        with pytest.raises(ProviderNotConfigured):
            factory()


class TestTheVerifiedDefaults:
    """The model ids this layer ships with, pinned so a change is a visible diff.

    All three were read from the vendors' own catalogues on 2026-09-12 (ADR 0024). They
    are defaults rather than constants in the request path — `CEREBRAS_MODEL`,
    `OPENROUTER_MODEL` and `GROQ_MODEL` override them — but a silent edit here would
    change what production asks for, so the values are asserted rather than trusted to
    review.
    """

    def test_cerebras_defaults_to_its_verified_production_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CEREBRAS_API_KEY", SECRET)

        assert CerebrasProvider().model == DEFAULT_CEREBRAS_MODEL == "gpt-oss-120b"

    def test_groq_defaults_to_its_verified_production_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GROQ_API_KEY", SECRET)

        assert GroqProvider().model == DEFAULT_GROQ_MODEL == "openai/gpt-oss-120b"

    def test_openrouter_defaults_to_its_verified_catalogue_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)

        assert OpenRouterProvider().model == DEFAULT_OPENROUTER_MODEL == "openai/gpt-oss-120b"

    def test_all_three_default_to_one_model_family(self) -> None:
        # Deliberate, and the reason is the citation gate: `[n]` markers against numbered
        # passages are a formatting contract, and a fallback from another model family
        # keeps it differently — so its answers get thrown away by the gate at exactly
        # the moment the primary is down. Same family, independent infrastructure.
        assert DEFAULT_CEREBRAS_MODEL.endswith("gpt-oss-120b")
        assert DEFAULT_OPENROUTER_MODEL.endswith("gpt-oss-120b")
        assert DEFAULT_GROQ_MODEL.endswith("gpt-oss-120b")

    def test_the_model_is_overridable_without_a_deploy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A vendor retiring a model must be answerable with a configuration change.
        monkeypatch.setenv("CEREBRAS_API_KEY", SECRET)
        monkeypatch.setenv("CEREBRAS_MODEL", "some-newer-model")

        assert CerebrasProvider().model == "some-newer-model"


class TestOpenRouterRouting:
    async def test_fallback_models_become_the_models_array(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # §17: OpenRouter's own ordered fallback runs *inside* what the outer chain counts
        # as one attempt, so a total OpenRouter outage still costs one link, not three.
        monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=ok_body())

        provider = OpenRouterProvider(
            model="vendor/primary",
            fallback_models=["vendor/second", "vendor/third"],
            transport=mock_transport(handler),
        )
        await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

        assert seen["model"] == "vendor/primary"
        assert seen["models"] == ["vendor/primary", "vendor/second", "vendor/third"]

    async def test_without_fallbacks_it_sends_a_plain_single_model_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=ok_body())

        provider = OpenRouterProvider(
            model="vendor/primary", fallback_models=[], transport=mock_transport(handler)
        )
        await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

        assert "models" not in seen

    async def test_the_fallback_list_is_empty_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An in-provider fallback is a second model slug to keep current, and a retired
        # one makes OpenRouter reject the whole request rather than degrading. Asserted
        # through what is sent rather than off an attribute: the wire is the contract.
        monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=ok_body())

        provider = OpenRouterProvider(transport=mock_transport(handler))
        await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

        assert "models" not in seen
        assert seen["model"] == DEFAULT_OPENROUTER_MODEL


class TestSecretsNeverEscape:
    async def test_no_key_reaches_a_log_line(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Every provider fails, so every logging branch in the chain runs: attempt,
        # fallback, and the final failure.
        monkeypatch.setenv("GROQ_API_KEY", SECRET)
        transport = chain(
            FakeProvider("cerebras", error=ProviderRefused("cerebras", "status 401")),
            FakeProvider("groq", error=ProviderUnavailable("groq", "status 500")),
        )

        with caplog.at_level(logging.DEBUG), pytest.raises(ServiceUnavailable):
            await ask(transport)

        assert caplog.records
        assert SECRET not in caplog.text

    async def test_no_prompt_or_answer_reaches_a_log_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # §4.9 matters more here than anywhere: the prompt contains the customer's
        # retrieved evidence, which is the one thing this layer holds in memory.
        transport = chain(FakeProvider("cerebras", answer="THE ANSWER TEXT [1]"))

        with caplog.at_level(logging.DEBUG):
            await transport.complete(system="SYSTEM TEXT", prompt="PROMPT TEXT")

        assert "PROMPT TEXT" not in caplog.text
        assert "SYSTEM TEXT" not in caplog.text
        assert "THE ANSWER TEXT" not in caplog.text

    def test_a_provider_error_never_carries_the_vendors_message(self) -> None:
        # An upstream error body can echo the prompt or carry an account identifier.
        error = ProviderRefused("groq", "status 401")

        assert "401" in str(error)
        assert SECRET not in str(error)

    def test_no_provider_key_is_reachable_from_the_browser(self) -> None:
        # A server-side key that reached a bundle would be public the moment it shipped.
        # The web app must not name these variables at all — and `NEXT_PUBLIC_*` is the
        # only mechanism that could inline one.
        web = REPO_ROOT / "apps" / "web"
        sources = [
            path
            for path in web.rglob("*.*")
            if path.suffix in {".ts", ".tsx", ".js", ".mjs", ".json"}
            and "node_modules" not in path.parts
            and ".next" not in path.parts
        ]

        assert sources, "no web sources found — the guard would pass vacuously"
        for path in sources:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name in KEY_ENVS:
                assert name not in text, f"{name} appears in {path}"


class TestConcurrency:
    async def test_simultaneous_requests_do_not_share_provider_state(self) -> None:
        # Each request builds its own chain and its own frozen request; the providers
        # hold no cross-request state. Twenty questions asked at once must each get their
        # own answer rather than one another's.
        async def one(index: int) -> str:
            cerebras = FakeProvider("cerebras", error=ProviderTimeout("cerebras"))
            groq = FakeProvider("groq", answer=f"answer {index} [1]")
            return await chain(cerebras, groq).complete(system="s", prompt=f"question {index}")

        results = await asyncio.gather(*(one(index) for index in range(20)))

        assert results == [f"answer {index} [1]" for index in range(20)]


class TestTheLayerImportsNoApplication:
    def test_jutsu_llm_does_not_import_an_app(self) -> None:
        """`apps/api` and `apps/worker` both depend on this package; it may depend on
        neither. An import either way round would make one app's deployment able to break
        the other's, and would put prompt composition or job state inside a layer whose
        whole claim is that it holds two strings.

        Parsed rather than grepped, deliberately. These modules *describe* their callers
        in prose — which seam they sit in, which gate runs above them — and that is the
        documentation a reader needs. A substring search would forbid explaining the
        layering in order to enforce it. `ast` sees imports and nothing else, including
        the ones hidden inside a function body.
        """
        package = REPO_ROOT / "packages" / "llm" / "src" / "jutsu_llm"
        sources = sorted(package.rglob("*.py"))

        assert sources, "no sources found — the guard would pass vacuously"
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
            for module in imported:
                root = module.split(".")[0]
                assert root not in ("jutsu_api", "jutsu_worker"), f"{module} imported by {path}"
