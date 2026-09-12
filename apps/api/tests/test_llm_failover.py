"""The provider chain: what it tries, in what order, and what it must never do (ADR 0023).

Three kinds of test, and the middle one is the one that would catch a real regression.

**The chain**, against scripted providers: order, fallback on each retryable class, the
attempt ceiling, the total budget, and the single most important property — that every
provider is handed the *identical* request, because a fallback answering a subtly
different question is worse than no fallback at all.

**The adapters**, against `httpx.MockTransport` and a scripted Anthropic client. These run
the real mapping code — 429, 503, 408, a malformed 200, an empty completion, a safety
refusal — rather than a fake that agrees with it. A hand-written stand-in for a status-code
branch proves that the stand-in is correct.

**The boundary**: no key in a log line, no key in an exception, no provider key anywhere a
browser could reach, and no session, tenant or principal reachable from this layer at all.

Nothing here makes a paid call. `test_llm_live_smoke.py` is the opt-in that does.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
from pathlib import Path
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
from jutsu_api import answers
from jutsu_api.llm import (
    DEFAULT_ORDER,
    CerebrasProvider,
    ClaudeProvider,
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
    build_chain,
    configured_order,
    provider_status,
)
from jutsu_api.llm.providers import DEFAULT_CEREBRAS_MODEL, DEFAULT_GROQ_MODEL
from jutsu_core.errors import ServiceUnavailable

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Every environment variable that can hold a provider credential. Used by the leak tests,
#: and listed once so a fifth provider cannot be added without this list noticing.
KEY_ENVS = ("ANTHROPIC_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY")

SECRET = "sk-test-DO-NOT-LOG-9f3a2b"


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
        # A. The overwhelmingly common case, and the one a fallback layer must not make
        # slower or more expensive: Claude answers, nobody else is asked.
        claude = FakeProvider("claude")
        cerebras = FakeProvider("cerebras")

        assert "claude" in await ask(chain(claude, cerebras))

        assert len(claude.requests) == 1
        assert cerebras.requests == []

    @pytest.mark.parametrize(
        "error",
        [
            ProviderTimeout("claude"),
            ProviderRateLimited("claude"),
            ProviderUnavailable("claude", "status 503"),
            ProviderRefused("claude", "status 400"),
        ],
        ids=["timeout", "rate_limited", "unavailable", "refused"],
    )
    async def test_every_provider_failure_class_falls_over(self, error: Exception) -> None:
        # B, C, D and H. Timeout, 429 and 5xx are the obvious ones. `refused` is the
        # deliberate decision: the provider is never retried, but the *chain* continues,
        # because one vendor's stricter validation or one unrotated key must not take
        # down a request three others would have answered.
        claude = FakeProvider("claude", error=error)
        cerebras = FakeProvider("cerebras")

        assert "cerebras" in await ask(chain(claude, cerebras))

    async def test_it_walks_the_whole_chain_until_something_answers(self) -> None:
        # E and F, in one: three failures and a fourth provider that works.
        claude = FakeProvider("claude", error=ProviderTimeout("claude"))
        cerebras = FakeProvider("cerebras", error=ProviderRateLimited("cerebras"))
        openrouter = FakeProvider("openrouter", error=ProviderUnavailable("openrouter"))
        groq = FakeProvider("groq")

        assert "groq" in await ask(chain(claude, cerebras, openrouter, groq))

        assert len(groq.requests) == 1

    async def test_when_every_provider_fails_the_caller_sees_the_existing_error(self) -> None:
        # G. `ServiceUnavailable` is what `/v1/ask` has always raised and what the error
        # envelope renders as a 503 — no schema change, no new failure mode to handle.
        transport = chain(
            FakeProvider("claude", error=ProviderUnavailable("claude")),
            FakeProvider("cerebras", error=ProviderUnavailable("cerebras")),
        )

        with pytest.raises(ServiceUnavailable):
            await ask(transport)

    async def test_the_error_never_names_a_provider(self) -> None:
        # §11. A caller must not learn which vendors JUTSU uses, let alone which was
        # unwell, from an error message.
        transport = chain(
            FakeProvider("claude", error=ProviderRateLimited("claude")),
            FakeProvider("groq", error=ProviderRateLimited("groq")),
        )

        with pytest.raises(ServiceUnavailable) as caught:
            await ask(transport)

        message = str(caught.value)
        for name in DEFAULT_ORDER:
            assert name not in message.lower()

    async def test_an_empty_chain_refuses_rather_than_crashing(self) -> None:
        with pytest.raises(ServiceUnavailable):
            await ask(chain())

    async def test_the_attempt_ceiling_is_honoured(self) -> None:
        # §27: one user request, a hard maximum number of paid attempts. With the ceiling
        # at two, the third provider is never asked even though it would have answered.
        third = FakeProvider("openrouter")
        transport = chain(
            FakeProvider("claude", error=ProviderTimeout("claude")),
            FakeProvider("cerebras", error=ProviderTimeout("cerebras")),
            third,
            max_attempts=2,
        )

        with pytest.raises(ServiceUnavailable):
            await ask(transport)

        assert third.requests == []


class TestFailureInjection:
    """§25, stated as sequences: who was actually called, in order, in each outage shape.

    The tests above prove each hop. These prove the *paths* — including the one that
    matters commercially, where nothing is wrong and nobody but Claude is paid.
    """

    @staticmethod
    def _providers(*errors: Exception | None) -> list[FakeProvider]:
        names = ["claude", "cerebras", "openrouter", "groq"]
        return [FakeProvider(name, error=error) for name, error in zip(names, errors, strict=True)]

    @pytest.mark.parametrize(
        ("errors", "expected_called", "answered_by"),
        [
            pytest.param(
                (None, None, None, None),
                ["claude"],
                "claude",
                id="claude-answers-nobody-else-is-paid",
            ),
            pytest.param(
                (ProviderTimeout("claude"), None, None, None),
                ["claude", "cerebras"],
                "cerebras",
                id="claude-timeout-to-cerebras",
            ),
            pytest.param(
                (ProviderRateLimited("claude"), ProviderRateLimited("cerebras"), None, None),
                ["claude", "cerebras", "openrouter"],
                "openrouter",
                id="two-rate-limited-to-openrouter",
            ),
            pytest.param(
                (
                    ProviderTimeout("claude"),
                    ProviderUnavailable("cerebras", "status 500"),
                    ProviderUnavailable("openrouter", "status 503"),
                    None,
                ),
                ["claude", "cerebras", "openrouter", "groq"],
                "groq",
                id="three-down-to-groq",
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

    async def test_the_request_is_identical_across_the_chain(self) -> None:
        # I, and §16. This is the property that makes a fallback answer *the same answer
        # to the same question*. A chain that trimmed the prompt, appended an error, or
        # re-retrieved between attempts would still pass every other test in this file.
        claude = FakeProvider("claude", error=ProviderTimeout("claude"))
        cerebras = FakeProvider("cerebras", error=ProviderTimeout("cerebras"))
        groq = FakeProvider("groq")
        transport = chain(claude, cerebras, groq)

        await ask(transport)

        seen = [claude.requests[0], cerebras.requests[0], groq.requests[0]]
        assert all(request == seen[0] for request in seen)
        assert seen[0].system == "SYSTEM"
        assert seen[0].prompt == "PROMPT"

    async def test_the_request_cannot_be_mutated_between_attempts(self) -> None:
        # Frozen by construction, so "the same request" is enforced rather than promised.
        request = LLMRequest(system="s", prompt="p")

        with pytest.raises(dataclasses.FrozenInstanceError):
            request.prompt = "edited"  # type: ignore[misc]

    def test_the_transport_cannot_reach_a_tenant_a_user_or_a_database(self) -> None:
        # J, asserted structurally rather than by outcome. The chain takes two strings.
        # There is no session, no principal and no org id to widen, which is what keeps
        # ACL filtering upstream where it belongs — the same shape as
        # `search_chunks(…)` taking no `org_id`.
        parameters = set(inspect.signature(FailoverTransport.complete).parameters)

        assert parameters == {"self", "system", "prompt"}
        for forbidden in ("session", "user_id", "org_id", "principal", "db"):
            assert forbidden not in parameters


class TestTheBudget:
    async def test_a_provider_never_gets_more_than_its_slice(self) -> None:
        claude = FakeProvider("claude")
        await ask(chain(claude, provider_timeout_s=7.0, total_timeout_s=30.0))

        assert claude.timeouts == [7.0]

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
        claude = FakeProvider("claude")
        await ask(chain(claude, provider_timeout_s=30.0, total_timeout_s=2.0))

        assert claude.timeouts == pytest.approx([2.0], abs=0.05)

    async def test_the_chain_stops_when_the_budget_is_gone(self) -> None:
        # M. A slow first provider must not be able to spend the whole request and then
        # have three more vendors tried anyway. The total is the outer bound.
        slow = FakeProvider("claude", error=ProviderTimeout("claude"), delay_s=0.3)
        never = FakeProvider("cerebras")
        transport = chain(slow, never, provider_timeout_s=0.2, total_timeout_s=0.25)

        with pytest.raises(ServiceUnavailable):
            await ask(transport)

        assert never.requests == []


class TestConfiguration:
    def test_the_default_order_is_claude_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LLM_PROVIDER_ORDER", raising=False)

        assert configured_order() == ("claude", "cerebras", "openrouter", "groq")

    def test_the_order_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Q. Ordering is deployment policy, not a constant somebody has to redeploy to
        # change.
        monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq, claude")

        assert configured_order() == ("groq", "claude")

    def test_an_unknown_name_is_dropped_rather_than_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq,gemini,claude")

        assert configured_order() == ("groq", "claude")

    def test_an_entirely_unknown_order_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A typo in one variable must not leave a deployment with no providers at all.
        monkeypatch.setenv("LLM_PROVIDER_ORDER", "gemini,llama")

        assert configured_order() == DEFAULT_ORDER

    def test_an_unconfigured_provider_is_left_out_of_the_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # §31. Claude and Groq configured, the middle two absent: the chain is the two
        # that exist, and nothing crashes over the two that do not.
        for name in KEY_ENVS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
        monkeypatch.setenv("GROQ_API_KEY", SECRET)

        assert [provider.name for provider in build_chain()] == ["claude", "groq"]

    def test_only_claude_configured_is_the_behaviour_that_shipped_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in KEY_ENVS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)

        assert [provider.name for provider in build_chain()] == ["claude"]

    def test_no_provider_configured_is_reported_honestly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in KEY_ENVS:
            monkeypatch.delenv(name, raising=False)

        assert build_chain() == []
        assert answers.answers_configured() is False

    def test_a_deployment_without_claude_can_still_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The gate used to read `ANTHROPIC_API_KEY` alone, which would have refused every
        # question on this deployment while a working provider sat configured.
        for name in KEY_ENVS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CEREBRAS_API_KEY", SECRET)

        assert answers.answers_configured() is True

    def test_openrouter_without_a_model_is_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OpenRouter ships no default model on purpose: its catalogue is a marketplace of
        # slugs that retire, and a stale default would look configured and fail every call.
        monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
        monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

        with pytest.raises(ProviderNotConfigured):
            OpenRouterProvider()

    def test_the_status_view_names_models_and_never_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in KEY_ENVS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("GROQ_API_KEY", SECRET)
        monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")

        rows = {row["provider"]: row for row in provider_status()}

        assert rows["groq"]["state"] == "configured"
        assert rows["groq"]["model"] == "openai/gpt-oss-120b"
        assert rows["claude"]["state"] == "not_configured"
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
        # L. Whatever the vendor's envelope looks like, what leaves the adapter is one
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

    async def test_a_safety_refusal_becomes_the_existing_refusal_sentinel(self) -> None:
        # Not a provider failure and not an answer: exactly what JUTSU already renders
        # when Anthropic's safety layer declines.
        provider = GroqProvider(
            transport=mock_transport(
                lambda r: httpx.Response(200, json=ok_body(content="", finish="content_filter"))
            )
        )

        response = await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

        assert response.content == answers.INSUFFICIENT_EVIDENCE

    async def test_the_key_is_sent_as_a_bearer_token_and_nowhere_else(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = request.content.decode()
            return httpx.Response(200, json=ok_body())

        provider = GroqProvider(transport=mock_transport(handler))
        await provider.generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

        assert seen["auth"] == f"Bearer {SECRET}"
        assert SECRET not in seen["body"]


class TestTheVerifiedDefaults:
    """The model ids this layer ships with, pinned so a change is a visible diff.

    Both were read from the vendor's own documentation on 2026-09-12 (ADR 0023). They are
    defaults rather than constants in the request path — `CEREBRAS_MODEL` and `GROQ_MODEL`
    override them — but a silent edit here would change what production asks for, so the
    values are asserted rather than trusted to review.
    """

    def test_cerebras_defaults_to_its_verified_production_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CEREBRAS_API_KEY", SECRET)
        monkeypatch.delenv("CEREBRAS_MODEL", raising=False)

        assert CerebrasProvider().model == DEFAULT_CEREBRAS_MODEL == "gpt-oss-120b"

    def test_groq_defaults_to_its_verified_production_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GROQ_API_KEY", SECRET)
        monkeypatch.delenv("GROQ_MODEL", raising=False)

        assert GroqProvider().model == DEFAULT_GROQ_MODEL == "openai/gpt-oss-120b"

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


class _FakeMessages:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    async def create(self, **kwargs: Any) -> Any:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _FakeAnthropic:
    """Stands in for `anthropic.AsyncAnthropic`, which no test may actually construct."""

    outcome: Any = None

    def __init__(self, **kwargs: Any) -> None:
        self.messages = _FakeMessages(type(self).outcome)

    async def close(self) -> None:
        return None


def _text_block(text: str) -> Any:
    return type("Block", (), {"type": "text", "text": text})()


def _message(text: str, *, stop_reason: str = "end_turn") -> Any:
    return type(
        "Message",
        (),
        {
            "content": [_text_block(text)],
            "stop_reason": stop_reason,
            "usage": type("Usage", (), {"input_tokens": 5, "output_tokens": 3})(),
        },
    )()


class TestTheClaudeAdapter:
    """The primary's error mapping, which is the one path already in production."""

    @pytest.fixture(autouse=True)
    def _claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
        # Patched on the SDK module itself, which is the object `providers.py` looks the
        # class up on at call time. Reaching through `providers.anthropic` would be the
        # same object and a re-export mypy refuses to see.
        monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAnthropic)

    async def test_a_successful_call_is_normalised(self) -> None:
        _FakeAnthropic.outcome = _message("grounded [1]")

        response = await ClaudeProvider().generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

        assert response.content == "grounded [1]"
        assert response.provider == "claude"
        assert response.input_tokens == 5

    async def test_a_safety_refusal_keeps_the_existing_sentinel(self) -> None:
        _FakeAnthropic.outcome = _message("", stop_reason="refusal")

        response = await ClaudeProvider().generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

        assert response.content == answers.INSUFFICIENT_EVIDENCE

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (
                anthropic.RateLimitError(
                    "rate",
                    response=httpx2.Response(429, request=httpx2.Request("POST", "https://a/b")),
                    body=None,
                ),
                ProviderRateLimited,
            ),
            (
                anthropic.APITimeoutError(request=httpx2.Request("POST", "https://a/b")),
                ProviderTimeout,
            ),
            (
                anthropic.APIConnectionError(request=httpx2.Request("POST", "https://a/b")),
                ProviderUnavailable,
            ),
            (
                anthropic.APIStatusError(
                    "boom",
                    response=httpx2.Response(503, request=httpx2.Request("POST", "https://a/b")),
                    body=None,
                ),
                ProviderUnavailable,
            ),
            (
                anthropic.APIStatusError(
                    "bad",
                    response=httpx2.Response(400, request=httpx2.Request("POST", "https://a/b")),
                    body=None,
                ),
                ProviderRefused,
            ),
        ],
        ids=["429", "timeout", "connection", "503", "400"],
    )
    async def test_sdk_errors_map_to_the_taxonomy(
        self, outcome: Exception, expected: type[Exception]
    ) -> None:
        _FakeAnthropic.outcome = outcome

        with pytest.raises(expected):
            await ClaudeProvider().generate(LLMRequest(system="s", prompt="p"), timeout_s=5)

    def test_it_uses_the_existing_model_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The primary's model is an existing production value. This layer must not rename
        # or re-default it.
        monkeypatch.setenv("JUTSU_ANSWER_MODEL", "claude-opus-5")

        assert ClaudeProvider().model == "claude-opus-5"

    def test_without_a_key_it_is_not_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with pytest.raises(ProviderNotConfigured):
            ClaudeProvider()


class TestSecretsNeverEscape:
    async def test_no_key_reaches_a_log_line(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # N. Every provider fails, so every logging branch in the chain runs: attempt,
        # fallback, and the final failure.
        monkeypatch.setenv("GROQ_API_KEY", SECRET)
        transport = chain(
            FakeProvider("claude", error=ProviderRefused("claude", "status 401")),
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
        transport = chain(FakeProvider("claude", answer="THE ANSWER TEXT [1]"))

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
        # O. A server-side key that reached a bundle would be public the moment it
        # shipped. The web app must not name these variables at all — and `NEXT_PUBLIC_*`
        # is the only mechanism that could inline one.
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
        # P. Each request builds its own chain and its own frozen request; the providers
        # hold no cross-request state. Twenty questions asked at once must each get their
        # own answer rather than one another's.
        async def one(index: int) -> str:
            claude = FakeProvider("claude", error=ProviderTimeout("claude"))
            groq = FakeProvider("groq", answer=f"answer {index} [1]")
            return await chain(claude, groq).complete(system="s", prompt=f"question {index}")

        results = await asyncio.gather(*(one(index) for index in range(20)))

        assert results == [f"answer {index} [1]" for index in range(20)]


class TestTheAnswerContractIsUnchanged:
    """A fallback answer is still an answer JUTSU is willing to show."""

    class _Evidence:
        chunk_id = "c1"
        document_id = "d1"
        document_title = "Handbook"
        source_system = "local"
        text = "Leave is approved by the line manager."

    async def test_a_fallback_answer_still_passes_the_citation_gate(self) -> None:
        # K. The gate runs downstream of the chain, so it applies to whichever provider
        # answered. Nothing about failover exempts a fallback from citing its evidence.
        transport = chain(
            FakeProvider("claude", error=ProviderTimeout("claude")),
            FakeProvider("groq", answer="The line manager approves leave [1]."),
        )

        outcome = await answers.synthesise_answer(
            transport, question="who approves leave?", evidence=[self._Evidence()]
        )

        assert outcome.insufficient_evidence is False
        assert [citation.chunk_id for citation in outcome.citations] == ["c1"]

    async def test_an_ungrounded_fallback_answer_is_refused_exactly_as_before(self) -> None:
        # The gate is not relaxed for a fallback: an uncited paragraph is refused after
        # the existing single retry, whichever vendor produced it.
        transport = chain(
            FakeProvider("claude", error=ProviderUnavailable("claude")),
            FakeProvider("groq", answer="Leave is approved by whoever you ask."),
        )

        outcome = await answers.synthesise_answer(
            transport, question="who approves leave?", evidence=[self._Evidence()]
        )

        assert outcome.insufficient_evidence is True
        assert outcome.answer is None
        assert outcome.attempts == 2

    async def test_the_transport_is_a_drop_in_for_the_one_it_replaced(self) -> None:
        # `synthesise_answer` calls `complete(system=…, prompt=…)` and nothing else.
        # Satisfying that signature is the whole contract, and it is what lets every
        # existing test in this suite keep injecting its own fake.
        transport = chain(FakeProvider("claude", answer="grounded [1]"))

        assert await transport.complete(system="s", prompt="p") == "grounded [1]"
