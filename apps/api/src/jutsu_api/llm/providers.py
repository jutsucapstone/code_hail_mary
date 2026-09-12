"""One adapter per vendor. Authenticate, shape, parse, classify — and nothing else.

**Model ids are configuration, never constants in this file's logic.** Every adapter reads
its model from the environment, because a vendor's catalogue changes on their schedule and
a model id compiled into a release is an outage waiting for someone else's deprecation
notice. The defaults below were read from each vendor's own documentation when this landed
and are recorded in ADR 0023 with the date; `docs/deploy.md` says to re-check them.

**A model id that no longer exists degrades to "skip this provider".** The vendor answers
4xx, the adapter raises `ProviderRefused`, and the chain moves on — see that class for why
continuing is the right reading rather than a way of hiding a mistake.

**Three of the four speak the same protocol.** Cerebras, OpenRouter and Groq are all
OpenAI-compatible `POST /v1/chat/completions`, so they share one implementation and differ
only in base URL, model and — for OpenRouter — an extra `models` array. Claude keeps the
official SDK it already used, because the existing error mapping in `answers.py` was
written against that SDK's exception types and re-deriving it over raw HTTP would be a
rewrite of the one path that is currently in production.

**Nothing here logs.** An adapter that logged would log per attempt, and the interesting
line is the chain's decision rather than each vendor's disappointment. The chain logs; this
file raises.
"""

from __future__ import annotations

import os
import time
from typing import Any, Final

import anthropic
import httpx

from jutsu_api.answers import INSUFFICIENT_EVIDENCE, answer_model
from jutsu_api.llm.types import (
    LLMRequest,
    LLMResponse,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderRefused,
    ProviderTimeout,
    ProviderUnavailable,
)

__all__ = [
    "CEREBRAS_BASE_URL",
    "DEFAULT_CEREBRAS_MODEL",
    "DEFAULT_GROQ_MODEL",
    "GROQ_BASE_URL",
    "OPENROUTER_BASE_URL",
    "CerebrasProvider",
    "ClaudeProvider",
    "GroqProvider",
    "OpenAICompatibleProvider",
    "OpenRouterProvider",
    "build_provider",
]

#: Read from each vendor's own API reference on 2026-09-12. See ADR 0023.
CEREBRAS_BASE_URL: Final = "https://api.cerebras.ai/v1/chat/completions"
GROQ_BASE_URL: Final = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_BASE_URL: Final = "https://openrouter.ai/api/v1/chat/completions"

#: Cerebras lists this in its model catalogue as a production model at 131k context on
#: paid tiers. Chosen over the smaller catalogue entry for reasoning and instruction
#: following, which is what the citation gate actually tests.
DEFAULT_CEREBRAS_MODEL: Final = "gpt-oss-120b"

#: Groq's model page marks this **production** (as opposed to preview) at 131k context.
#: Deliberately the same model family as the Cerebras default: two independent providers
#: serving one family means a fallback answers the citation gate the way the gate was
#: tuned for, instead of introducing a second set of formatting habits at the worst moment.
DEFAULT_GROQ_MODEL: Final = "openai/gpt-oss-120b"

#: **OpenRouter ships no default, on purpose.** Its catalogue is a marketplace of slugs
#: that appear and retire continuously, and a stale default would look configured and fail
#: every time. Unset means the provider is not configured and is skipped, which is the
#: honest state for "nobody has chosen a model yet".
_OPENROUTER_MODEL_ENV: Final = "OPENROUTER_MODEL"

#: Finish reasons that mean the vendor's safety layer declined rather than the model
#: answering. Mapped to JUTSU's existing refusal sentinel so the gate downstream treats it
#: exactly as it has always treated an Anthropic refusal.
_REFUSAL_REASONS: Final = frozenset({"content_filter", "refusal", "safety"})


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


class ClaudeProvider:
    """Anthropic, through the official SDK — the path that is in production today.

    The error mapping is the one `AnthropicTransport` already had, translated from
    `ServiceUnavailable` (an HTTP concern) into the provider taxonomy (a chain concern).
    Nothing about the request changes: same model, same `max_tokens`, same system and
    prompt strings, thinking left at the model's default.
    """

    name = "claude"

    def __init__(self, *, model: str | None = None) -> None:
        if not _env("ANTHROPIC_API_KEY"):
            raise ProviderNotConfigured(self.name, "ANTHROPIC_API_KEY is not set")
        # `answer_model()` rather than a constant: `JUTSU_ANSWER_MODEL` is an existing
        # production value and this layer must not quietly rename or re-default it.
        self._model = model or answer_model()

    @property
    def model(self) -> str:
        return self._model

    async def generate(self, request: LLMRequest, *, timeout_s: float) -> LLMResponse:
        client = anthropic.AsyncAnthropic(timeout=timeout_s)
        started = time.monotonic()
        try:
            response = await client.messages.create(
                model=self._model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[{"role": "user", "content": request.prompt}],
            )
        except anthropic.APITimeoutError as exc:
            raise ProviderTimeout(self.name) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderRateLimited(self.name) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailable(self.name, "connection") from exc
        except anthropic.APIStatusError as exc:
            # The provider's own message can carry request details; classify, never
            # forward — the rule `answers.py` already followed.
            if exc.status_code >= 500 or exc.status_code == 408:
                raise ProviderUnavailable(self.name, f"status {exc.status_code}") from exc
            raise ProviderRefused(self.name, f"status {exc.status_code}") from exc
        finally:
            await client.close()

        elapsed_ms = int((time.monotonic() - started) * 1000)

        if response.stop_reason == "refusal":
            # The safety layer declined. Not an evidence problem and not a provider fault:
            # the honest rendering is the refusal JUTSU already renders.
            return LLMResponse(
                content=INSUFFICIENT_EVIDENCE,
                provider=self.name,
                model=self._model,
                latency_ms=elapsed_ms,
                finish_reason="refusal",
            )

        content = "".join(block.text for block in response.content if block.type == "text")
        if not content.strip():
            raise ProviderUnavailable(self.name, "empty completion")

        usage = getattr(response, "usage", None)
        return LLMResponse(
            content=content,
            provider=self.name,
            model=self._model,
            latency_ms=elapsed_ms,
            finish_reason=response.stop_reason,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )


class OpenAICompatibleProvider:
    """Cerebras, Groq and OpenRouter: one protocol, three base URLs.

    **A fresh client per call, deliberately.** An `AsyncClient` held at module scope is a
    connection pool bound to the event loop that created it, and this repository has
    already paid for that lesson twice — the database engine and, last week, the Neo4j
    driver, where a pool outliving its loop failed a test thirty minutes into preflight.
    A request that is already spending seconds on a model call can afford a handshake.
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key_env: str,
        model: str,
        extra_body: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        key = _env(api_key_env)
        if not key:
            raise ProviderNotConfigured(name, f"{api_key_env} is not set")
        if not model:
            raise ProviderNotConfigured(name, "no model configured")
        self.name = name
        self._base_url = base_url
        self._key = key
        self._model = model
        self._extra_body = extra_body or {}
        #: The one seam in this file, and the same one every paid provider in this
        #: repository has: a test supplies an `httpx.MockTransport` and exercises the
        #: real status-code mapping — 429, 503, a malformed 200, a connection reset —
        #: against the real adapter. Without it those branches could only be tested by
        #: reimplementing them in a fake, which proves nothing about this code.
        self._transport = transport

    @property
    def model(self) -> str:
        return self._model

    def _body(self, request: LLMRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            # The system prompt as a system message: the same text Claude receives in its
            # `system` parameter, in the place this protocol puts it. No rewording, no
            # extra instructions, nothing appended — §28.
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        body.update(self._extra_body)
        return body

    async def generate(self, request: LLMRequest, *, timeout_s: float) -> LLMResponse:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_s, transport=self._transport) as client:
                response = await client.post(
                    self._base_url,
                    headers={
                        "Authorization": f"Bearer {self._key}",
                        "Content-Type": "application/json",
                    },
                    json=self._body(request),
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(self.name) from exc
        except httpx.HTTPError as exc:
            # Connection reset, DNS failure, protocol error — every transport-level
            # problem, and none of them carry a useful message worth forwarding.
            raise ProviderUnavailable(self.name, "connection") from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)

        if response.status_code == 429:
            raise ProviderRateLimited(self.name)
        if response.status_code == 408:
            raise ProviderTimeout(self.name)
        if response.status_code >= 500:
            raise ProviderUnavailable(self.name, f"status {response.status_code}")
        if response.status_code >= 400:
            raise ProviderRefused(self.name, f"status {response.status_code}")

        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"] or ""
            finish_reason = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            # A 200 whose body is not the shape this protocol promises is a provider
            # malfunction, not an answer. Nothing from the body reaches the message.
            raise ProviderUnavailable(self.name, "malformed response") from exc

        if finish_reason in _REFUSAL_REASONS:
            return LLMResponse(
                content=INSUFFICIENT_EVIDENCE,
                provider=self.name,
                model=self._model,
                latency_ms=elapsed_ms,
                finish_reason=str(finish_reason),
            )

        if not content.strip():
            raise ProviderUnavailable(self.name, "empty completion")

        usage = payload.get("usage") or {}
        return LLMResponse(
            content=content,
            provider=self.name,
            model=self._model,
            latency_ms=elapsed_ms,
            finish_reason=str(finish_reason) if finish_reason else None,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )


class CerebrasProvider(OpenAICompatibleProvider):
    def __init__(
        self, *, model: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(
            name="cerebras",
            base_url=CEREBRAS_BASE_URL,
            api_key_env="CEREBRAS_API_KEY",
            model=model or _env("CEREBRAS_MODEL") or DEFAULT_CEREBRAS_MODEL,
            transport=transport,
        )


class GroqProvider(OpenAICompatibleProvider):
    def __init__(
        self, *, model: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(
            name="groq",
            base_url=GROQ_BASE_URL,
            api_key_env="GROQ_API_KEY",
            model=model or _env("GROQ_MODEL") or DEFAULT_GROQ_MODEL,
            transport=transport,
        )


class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter, with its own ordered model fallback inside our single attempt.

    OpenRouter's `models` array is an in-provider fallback: it tries them in order and
    bills for whichever served. That is a second layer of resilience *inside* what JUTSU
    counts as one provider attempt, which is exactly how §17 wants it — the outer chain
    still treats OpenRouter as one link, so a total OpenRouter outage costs one attempt
    rather than several.

    `OPENROUTER_FALLBACK_MODELS` is a comma-separated list appended after the primary.
    Empty is normal and sends a plain single-model request.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        fallback_models: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        primary = model or _env(_OPENROUTER_MODEL_ENV)
        fallbacks = fallback_models
        if fallbacks is None:
            # Commas or semicolons — see `failover.split_list` for why the second spelling
            # exists (it is what makes this settable through `gcloud run deploy`).
            from jutsu_api.llm.failover import split_list

            fallbacks = split_list(_env("OPENROUTER_FALLBACK_MODELS"))

        extra: dict[str, Any] = {}
        if primary and fallbacks:
            extra["models"] = [primary, *fallbacks]

        super().__init__(
            name="openrouter",
            base_url=OPENROUTER_BASE_URL,
            api_key_env="OPENROUTER_API_KEY",
            model=primary,
            extra_body=extra,
            transport=transport,
        )


#: Name to constructor. The only place a provider name becomes a class, so
#: `LLM_PROVIDER_ORDER` is validated against exactly this set and a typo is a startup-time
#: complaint rather than a provider that silently never runs.
_REGISTRY: Final[dict[str, type]] = {
    "claude": ClaudeProvider,
    "cerebras": CerebrasProvider,
    "openrouter": OpenRouterProvider,
    "groq": GroqProvider,
}


def build_provider(name: str) -> Any:
    """Construct one provider by name, or raise `ProviderNotConfigured`.

    Construction is where "is this deployment able to use this vendor" is decided — a
    missing key or model raises here, so the chain can leave it out entirely instead of
    spending one of its four attempts discovering the same thing over the network.
    """
    factory = _REGISTRY.get(name)
    if factory is None:
        raise ProviderNotConfigured(name, "unknown provider")
    return factory()
