"""What a provider is asked, what it answers, and how it is allowed to fail.

Three small types and a taxonomy of errors. Nothing here talks to a network, so every
classification decision in this file can be tested without a provider, which is the point:
**the failure taxonomy is the whole design.** A chain that falls over to the next provider
on the wrong error either hides a bug in JUTSU by asking four vendors the same malformed
question, or gives up on an outage it could have ridden out.

**`LLMRequest` is deliberately the shape the existing transport already has.** JUTSU
composes the system prompt, the numbered evidence passages and the conversation preamble
into two strings *before* anything provider-shaped is reached — `answers.py` does it in
`_compose_prompt`, and the citation gate downstream resolves markers against the evidence
list it built. So a request here carries `system` and `prompt`, not a `retrieved_context`
field that nothing would populate. Inventing richer fields would mean either duplicating
prompt assembly at the provider layer or shipping fields that are always empty, and both
are worse than describing what actually travels (ADR 0023).

That is also what makes "every provider receives semantically equivalent input" exact
rather than approximate: every provider receives the *identical* two strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Protocol

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "ProviderError",
    "ProviderNotConfigured",
    "ProviderRateLimited",
    "ProviderRefused",
    "ProviderTimeout",
    "ProviderUnavailable",
]

#: What `AnthropicTransport` has always asked for. Carried on the request rather than read
#: from configuration inside each adapter, so four providers cannot drift into generating
#: different lengths for the same question.
DEFAULT_MAX_TOKENS: Final = 4096


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """One normalised generation request.

    Frozen, because it is handed to several providers in turn and a chain whose second
    attempt could see a mutated request would be the subtlest possible way to break
    "every fallback receives exactly the same input".
    """

    system: str
    prompt: str
    max_tokens: int = DEFAULT_MAX_TOKENS
    #: `None` means "the provider's own default". JUTSU has never set a temperature on the
    #: answer path and setting one here would change answers on the primary provider,
    #: which this layer exists not to do.
    temperature: float | None = None
    #: Opaque, non-sensitive labels for logging — a request id, never a user or a tenant.
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """What a provider answered, plus what it cost to get it.

    `content` is the only field the application uses; everything else is observability.
    The answer path takes `.content` and proceeds exactly as it did when there was one
    provider — the citation gate, the retry, the refusal are all downstream and unchanged.
    """

    content: str
    provider: str
    model: str
    latency_ms: int
    finish_reason: str | None = None
    #: Provider-reported token counts where they are given, for cost accounting. Never
    #: estimated: an invented number is worse than an absent one (§4.8 of the spec).
    input_tokens: int | None = None
    output_tokens: int | None = None


class ProviderError(Exception):
    """A provider did not produce an answer.

    Carries the provider's name and a short, non-sensitive class for logs. It never
    carries the provider's message body: an upstream error string can contain a request
    id, a prompt echo, an account identifier or — on a misconfigured gateway — a header
    (§4.9). What the chain needs is which provider failed and whether to try the next one.
    """

    #: Whether the *chain* should try the next provider. Distinct from "retry this
    #: provider", which the chain never does.
    fall_over: bool = True
    #: A stable label for logs and health output. Never interpolated from a response.
    error_class: str = "error"

    def __init__(self, provider: str, detail: str = "") -> None:
        self.provider = provider
        super().__init__(f"{provider}: {detail or self.error_class}")


class ProviderTimeout(ProviderError):
    """The provider did not answer inside its slice of the request budget."""

    error_class = "timeout"


class ProviderRateLimited(ProviderError):
    """429, or a provider-specific "overloaded". The next provider may well be free."""

    error_class = "rate_limited"


class ProviderUnavailable(ProviderError):
    """5xx, a connection reset, a DNS failure, or a response with no content at all.

    An empty completion belongs here rather than downstream: a provider that returns
    nothing has malfunctioned, and handing "" to the citation gate would turn a provider
    fault into `insufficient_evidence` — an answer of "the evidence does not support
    this", produced by a provider that never read it.
    """

    error_class = "unavailable"


class ProviderRefused(ProviderError):
    """A 4xx that is not 429: a bad key, a model this account cannot use, a rejected body.

    **The chain continues, and that is a deliberate reading of a rule worth restating.**
    Retrying *this* provider cannot help — the same request is refused identically every
    time — so it is never retried. But refusing to try the *next* provider would let one
    vendor's stricter validation, or one unrotated key, take down a request three other
    providers would have answered.

    The cost is stated rather than hidden: a request that is genuinely malformed by JUTSU
    is refused four times instead of once. That is bounded, fast (no retries, no backoff)
    and loud — every attempt logs `error_class=refused` with the provider's name, and the
    admin diagnostic shows a provider that refuses everything. It is not hidden from
    anybody looking, which is what "do not hide programming errors" actually requires.
    """

    error_class = "refused"


class ProviderNotConfigured(ProviderError):
    """No key, or no model id. Not an attempt, not a failure — a deployment fact.

    Raised at construction rather than on the request path, so an unconfigured provider is
    absent from the chain instead of consuming one of its four attempts.
    """

    fall_over = True
    error_class = "not_configured"


class LLMProvider(Protocol):
    """One vendor, behind one method.

    Adapters do exactly four things: authenticate, shape the request, parse the response,
    and translate the vendor's failures into the taxonomy above. They do not retry, do not
    know about each other, do not touch a database, and never see a tenant id — everything
    about authorization, retrieval and citations happened before the chain was entered and
    happens again after it returns (ADR 0023).
    """

    @property
    def name(self) -> str:
        """Stable identifier used in configuration, logs and health output."""
        ...

    @property
    def model(self) -> str:
        """The model this adapter will ask for. Configuration, never hard-coded."""
        ...

    async def generate(self, request: LLMRequest, *, timeout_s: float) -> LLMResponse:
        """Answer, or raise a `ProviderError`. Never raise anything else."""
        ...
