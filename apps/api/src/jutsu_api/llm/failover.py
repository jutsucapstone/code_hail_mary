"""The provider chain: one request, several vendors, in order, until one answers.

    LLMRequest ──▶ claude ──fail──▶ cerebras ──fail──▶ openrouter ──fail──▶ groq
                     │                 │                   │                  │
                     └─────────────────┴───────────────────┴──────────────────┘
                                              │
                                       first answer wins

**Sequential, never speculative.** Four vendors asked at once would answer faster and cost
four times as much for every question, including the overwhelming majority that Claude
answers on the first try. One request is one attempt per provider, bounded by
`LLM_MAX_PROVIDER_ATTEMPTS`, and the chain stops at the first answer (§27).

**The chain is a transport, not a pipeline.** It implements the `AnswerTransport` protocol
that `answers.py` has always called, so it sits strictly *below* prompt composition and
strictly *above* nothing at all. Retrieval, ACL filtering, evidence numbering, the citation
gate, the one retry and the refusal all live upstream and downstream exactly where they
were; this file cannot see a tenant, a document or a principal, and does not know that
retrieval exists (ADR 0023).

**Every provider receives the identical request object**, frozen, built once before the
loop. There is no path by which attempt two differs from attempt one — no appended error
text, no shortened prompt, no dropped context.

**The budget is the outer bound.** Each provider gets the smaller of its own timeout and
whatever remains of the total, so a chain cannot outlive `LLM_TOTAL_TIMEOUT_SECONDS`
however many providers are configured, and a first provider that hangs cannot spend the
whole budget and leave the others nothing.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Sequence
from typing import Final

from jutsu_core.errors import ServiceUnavailable

from jutsu_api.llm.providers import build_provider
from jutsu_api.llm.types import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderError,
    ProviderNotConfigured,
)

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_ORDER",
    "DEFAULT_PROVIDER_TIMEOUT_S",
    "DEFAULT_TOTAL_TIMEOUT_S",
    "FailoverTransport",
    "build_chain",
    "configured_order",
    "provider_status",
    "split_list",
]

#: Counts, timings, provider names and error classes. Never a prompt, never an answer,
#: never a key — §4.9 applies here more than anywhere, because this module holds the one
#: string in the request path that contains the customer's retrieved evidence.
logger = logging.getLogger("jutsu.api.llm")

#: Claude first, and that is the product decision this whole layer is arranged around:
#: the primary is unchanged and the rest exist for the minutes when it is not answering.
DEFAULT_ORDER: Final = ("claude", "cerebras", "openrouter", "groq")

DEFAULT_PROVIDER_TIMEOUT_S: Final = 30.0
DEFAULT_TOTAL_TIMEOUT_S: Final = 90.0
DEFAULT_MAX_ATTEMPTS: Final = 4

#: The sentences JUTSU already shows when the answer service is unavailable. Reused rather
#: than replaced: a caller must not be able to tell from the wording that a fallback chain
#: exists, let alone which vendor was unwell (§11).
_MESSAGES: Final[dict[str, str]] = {
    "rate_limited": "The answer service is briefly over capacity. Try again shortly.",
    "timeout": "The answer service did not respond.",
    "unavailable": "The answer service is unreachable.",
    "refused": "The answer service did not respond.",
}
_DEFAULT_MESSAGE: Final = "The answer service did not respond."


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    # Zero or negative is a mistyped bound, not "unlimited". The same stance
    # `EMBEDDING_TOKEN_BUDGET=0` takes: a guardrail must not disappear through a typo.
    return value if value > 0 else default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def split_list(raw: str) -> list[str]:
    """A configured list, separated by commas **or** semicolons.

    Both, and the second one is not decoration: `gcloud run deploy --set-env-vars` splits
    its own argument on commas, so a comma-separated value cannot be set that way without
    switching the whole flag to gcloud's `^@^` alternate-delimiter form — rewriting one
    long, production-critical line to configure one list. A semicolon costs nothing here
    and keeps the deployment command boring (`docs/deploy.md` §13).
    """
    normalised = raw.replace(";", ",")
    return [entry.strip() for entry in normalised.split(",") if entry.strip()]


def configured_order() -> tuple[str, ...]:
    """The provider order for this deployment, from `LLM_PROVIDER_ORDER`.

    Unknown names are dropped rather than raising: an operator adding a vendor JUTSU does
    not implement should not take the answer service down, and `provider_status` shows
    exactly which names were understood.
    """
    raw = os.environ.get("LLM_PROVIDER_ORDER", "").strip()
    if not raw:
        return DEFAULT_ORDER
    names = tuple(entry.lower() for entry in split_list(raw))
    known = tuple(name for name in names if name in DEFAULT_ORDER)
    return known or DEFAULT_ORDER


def build_chain(order: Sequence[str] | None = None) -> list[LLMProvider]:
    """Every configured provider, in order. Unconfigured ones are left out.

    Built per request rather than cached, and that is two decisions at once: a rotated key
    or a changed model takes effect without a restart, and there is no shared mutable
    provider state for concurrent requests to contend over (§24-P). It costs a handful of
    environment reads on a path that is about to spend seconds on a model.
    """
    chain: list[LLMProvider] = []
    for name in order or configured_order():
        try:
            chain.append(build_provider(name))
        except ProviderNotConfigured:
            # Absent, not failed. No log line: an unconfigured provider is a deployment
            # fact that would otherwise be repeated on every question ever asked.
            continue
    return chain


def provider_status(order: Sequence[str] | None = None) -> list[dict[str, str]]:
    """Which providers this deployment can use, for the admin diagnostic.

    Names and model ids only — never a key, never a fragment of one, never a header. A
    model id is not a secret: it is on every invoice and in every vendor's public
    catalogue.
    """
    status: list[dict[str, str]] = []
    for name in order or configured_order():
        try:
            provider = build_provider(name)
        except ProviderNotConfigured:
            status.append({"provider": name, "state": "not_configured", "model": ""})
            continue
        status.append({"provider": name, "state": "configured", "model": provider.model})
    return status


class FailoverTransport:
    """An `AnswerTransport` that tries several providers in order.

    Drop-in for `AnthropicTransport`: same method, same arguments, same return type, and
    the same `ServiceUnavailable` on the way out when nothing can answer. A deployment
    with only Claude configured behaves exactly as it did before this existed — one
    provider, one attempt, the same error sentences (§31).
    """

    __slots__ = ("_max_attempts", "_providers", "_timeout_s", "_total_s")

    def __init__(
        self,
        providers: Sequence[LLMProvider] | None = None,
        *,
        provider_timeout_s: float | None = None,
        total_timeout_s: float | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self._providers = list(providers) if providers is not None else build_chain()
        self._timeout_s = provider_timeout_s or _float_env(
            "LLM_PROVIDER_TIMEOUT_SECONDS", DEFAULT_PROVIDER_TIMEOUT_S
        )
        self._total_s = total_timeout_s or _float_env(
            "LLM_TOTAL_TIMEOUT_SECONDS", DEFAULT_TOTAL_TIMEOUT_S
        )
        self._max_attempts = max_attempts or _int_env(
            "LLM_MAX_PROVIDER_ATTEMPTS", DEFAULT_MAX_ATTEMPTS
        )

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self._providers)

    async def complete(self, *, system: str, prompt: str) -> str:
        """The `AnswerTransport` contract: text in, text out, or `ServiceUnavailable`."""
        return (await self.generate(LLMRequest(system=system, prompt=prompt))).content

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """The same call, with the provider and the cost attached.

        Kept separate from `complete` so observability does not have to be smuggled
        through a string. Nothing in the application reads it yet; the health diagnostic
        and future cost accounting will.
        """
        if not self._providers:
            # No vendor at all. The same 503 an unconfigured deployment has always
            # answered — `answers_configured()` normally catches this before a budget is
            # spent, and this is the backstop for a key that vanished mid-process.
            logger.warning("%s", {"event": "llm_no_providers_configured"})
            raise ServiceUnavailable(
                "Answers are not configured for this deployment yet. Retrieval still "
                "works — an administrator must add the answer provider's credentials."
            )

        deadline = time.monotonic() + self._total_s
        attempts = 0
        last_class = "unavailable"
        eligible = self._providers[: self._max_attempts]

        for index, provider in enumerate(eligible):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "%s",
                    {
                        "event": "llm_budget_exhausted",
                        "attempts": attempts,
                        "next": provider.name,
                    },
                )
                break

            slice_s = min(self._timeout_s, remaining)
            attempts += 1
            started = time.monotonic()
            try:
                response = await provider.generate(request, timeout_s=slice_s)
            except ProviderError as error:
                last_class = error.error_class
                logger.warning(
                    "%s",
                    {
                        "event": "llm_provider_attempt",
                        "provider": provider.name,
                        "model": provider.model,
                        "success": False,
                        "error_class": error.error_class,
                        "latency_ms": int((time.monotonic() - started) * 1000),
                    },
                )
                nxt = eligible[index + 1] if index + 1 < len(eligible) else None
                if nxt is not None:
                    logger.info(
                        "%s",
                        {
                            "event": "llm_provider_fallback",
                            "from": provider.name,
                            "to": nxt.name,
                            "reason": error.error_class,
                        },
                    )
                continue

            logger.info(
                "%s",
                {
                    "event": "llm_provider_attempt",
                    "provider": provider.name,
                    "model": provider.model,
                    "success": True,
                    "latency_ms": response.latency_ms,
                },
            )
            logger.info(
                "%s",
                {
                    "event": "llm_request_success",
                    "provider": response.provider,
                    "model": response.model,
                    "fallback_used": index > 0,
                    "attempts": attempts,
                    "latency_ms": response.latency_ms,
                },
            )
            return response

        logger.error(
            "%s",
            {
                "event": "llm_request_failed",
                "attempts": attempts,
                "last_error_class": last_class,
                "providers": list(self.providers),
            },
        )
        # One of the sentences JUTSU already shows. The caller learns that answers are
        # unavailable, not how many vendors were asked or which of them was unwell.
        raise ServiceUnavailable(_MESSAGES.get(last_class, _DEFAULT_MESSAGE))
