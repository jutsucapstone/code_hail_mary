"""One adapter per vendor. Authenticate, shape, parse, classify — and nothing else.

**All three speak the same protocol**, which is why there is one implementation and three
thin subclasses. Cerebras, OpenRouter and Groq each serve an OpenAI-compatible
`POST /v1/chat/completions`, so they differ only in base URL, model and — for OpenRouter —
an extra `models` array. Three vendor SDKs would have been three dependency trees and
three sets of exception types to translate, for what is one authenticated POST each.

**Model ids are configuration, never constants in this file's logic.** Every adapter reads
its model from the environment, because a vendor's catalogue changes on their schedule and
a model id compiled into a release is an outage waiting for someone else's deprecation
notice. The defaults below were verified against each vendor's own source on 2026-09-12 and
are recorded in ADR 0024; `docs/deploy.md` §13 says to re-check them.

**A model id that no longer exists degrades to "skip this provider".** The vendor answers
4xx, the adapter raises `ProviderRefused`, and the chain moves on — see that class for why
continuing is the right reading rather than a way of hiding a mistake. JUTSU has already
been on the other side of this: with one vendor and one model id, a model that stopped
being served returned 400 to every call for five days, and because there was nowhere to
fall over to, every answer in production was a 503 (ADR 0024).

**Nothing here logs.** An adapter that logged would log per attempt, and the interesting
line is the chain's decision rather than each vendor's disappointment. The chain logs; this
file raises.
"""

from __future__ import annotations

import os
import time
from typing import Any, Final

import httpx

from jutsu_llm.types import (
    INSUFFICIENT_EVIDENCE,
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
    "DEFAULT_OPENROUTER_MODEL",
    "GROQ_BASE_URL",
    "OPENROUTER_BASE_URL",
    "CerebrasProvider",
    "GroqProvider",
    "OpenAICompatibleProvider",
    "OpenRouterProvider",
    "build_provider",
]

#: Read from each vendor's own API reference on 2026-09-12. See ADR 0024.
CEREBRAS_BASE_URL: Final = "https://api.cerebras.ai/v1/chat/completions"
GROQ_BASE_URL: Final = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_BASE_URL: Final = "https://openrouter.ai/api/v1/chat/completions"

#: **One model family across all three providers, and that is the deliberate choice.**
#:
#: The citation gate is what decides whether an answer is shown at all: the model must emit
#: `[n]` markers against numbered passages, or emit `INSUFFICIENT_EVIDENCE` and nothing
#: else. That is a formatting contract, and models from different families keep it
#: differently — so a fallback from a different family does not "degrade gracefully", it
#: gets its answers thrown away by the gate at exactly the moment the primary is down.
#:
#: One family served by three independent companies keeps the formatting constant while
#: keeping the *infrastructure* independent, which is the thing an availability layer
#: actually needs. The cost, stated rather than hidden: a flaw in the model family itself
#: would affect all three at once. Each is separately overridable through its own
#: environment variable for exactly that case.
#:
#: Cerebras lists `gpt-oss-120b` in its catalogue as production, 131k context on paid tiers.
DEFAULT_CEREBRAS_MODEL: Final = "gpt-oss-120b"

#: Groq's model page marks `openai/gpt-oss-120b` **production**, as opposed to the preview
#: models it explicitly says not to use in production. 131k context.
DEFAULT_GROQ_MODEL: Final = "openai/gpt-oss-120b"

#: Verified against OpenRouter's live catalogue (`GET /api/v1/models`, 2026-09-12): present,
#: 131,072 context, 117,964 max completion tokens, `response_format` supported, and priced
#: at $0.04/$0.17 per million tokens — the cheapest tier, so a fallback cannot turn into a
#: cost incident the way a frontier default at $5/$30 would.
DEFAULT_OPENROUTER_MODEL: Final = "openai/gpt-oss-120b"

_OPENROUTER_MODEL_ENV: Final = "OPENROUTER_MODEL"

#: Finish reasons that mean the vendor's safety layer declined rather than the model
#: answering. Mapped to the refusal sentinel so the gate downstream treats it exactly as it
#: treats a model that decided the evidence was insufficient.
_REFUSAL_REASONS: Final = frozenset({"content_filter", "refusal", "safety"})


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


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
            # The system prompt as a system message: exactly the string `answers.py`
            # composed, in the place this protocol puts it. No rewording, no extra
            # instructions, nothing appended. An adapter converts shape, never content.
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

    `OPENROUTER_FALLBACK_MODELS` is a list appended after the primary. Empty is the
    default and sends a plain single-model request: an in-provider fallback is a second
    model id to keep current, and a slug that has retired makes OpenRouter reject the
    whole request rather than degrading. `docs/deploy.md` §13 lists verified candidates
    with their prices for anyone who wants one.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        fallback_models: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        primary = model or _env(_OPENROUTER_MODEL_ENV) or DEFAULT_OPENROUTER_MODEL
        fallbacks = fallback_models
        if fallbacks is None:
            # Commas or semicolons — see `failover.split_list` for why the second spelling
            # exists (it is what makes this settable through `gcloud run deploy`).
            from jutsu_llm.failover import split_list

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
