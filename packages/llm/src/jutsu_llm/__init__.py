"""JUTSU's model access: three providers, one chain, one normalised response (ADR 0024).

    types      the request, the response, the refusal sentinel, the failure taxonomy
    providers  one adapter per vendor: authenticate, shape, parse, classify
    failover   the ordered chain, its budget, and what it logs

**This package is an availability layer and nothing else.** It sits inside the transport
seam that `jutsu_api.answers` and `jutsu_worker.extraction` both call, which means every
question about *what* the model is asked — the system prompt, the retrieved passages, the
numbered evidence, the extraction schema — is settled before anything here runs, and every
question about whether the answer may be used — the citation gate, the quote gate, the
refusal — is settled after it returns.

Nothing in this package sees a tenant, a principal, a document or a chunk. It takes two
strings and returns one.

**A workspace package rather than a module inside `apps/api`**, because the worker's
nightly extraction needs the same chain and an app may not import another app. One chain
for both is the point: two provider frameworks would be two failure taxonomies, two
ordering rules and two sets of credentials to keep in step.
"""

from jutsu_llm.failover import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_ORDER,
    DEFAULT_PROVIDER_TIMEOUT_S,
    DEFAULT_TOTAL_TIMEOUT_S,
    AllProvidersFailed,
    FailoverTransport,
    build_chain,
    configured_order,
    provider_status,
    split_list,
)
from jutsu_llm.providers import (
    DEFAULT_CEREBRAS_MODEL,
    DEFAULT_GROQ_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    CerebrasProvider,
    GroqProvider,
    OpenAICompatibleProvider,
    OpenRouterProvider,
    build_provider,
)
from jutsu_llm.types import (
    DEFAULT_MAX_TOKENS,
    INSUFFICIENT_EVIDENCE,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderRefused,
    ProviderTimeout,
    ProviderUnavailable,
)

__all__ = [
    "DEFAULT_CEREBRAS_MODEL",
    "DEFAULT_GROQ_MODEL",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_OPENROUTER_MODEL",
    "DEFAULT_ORDER",
    "DEFAULT_PROVIDER_TIMEOUT_S",
    "DEFAULT_TOTAL_TIMEOUT_S",
    "INSUFFICIENT_EVIDENCE",
    "AllProvidersFailed",
    "CerebrasProvider",
    "FailoverTransport",
    "GroqProvider",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "OpenAICompatibleProvider",
    "OpenRouterProvider",
    "ProviderError",
    "ProviderNotConfigured",
    "ProviderRateLimited",
    "ProviderRefused",
    "ProviderTimeout",
    "ProviderUnavailable",
    "any_provider_configured",
    "build_chain",
    "build_provider",
    "configured_order",
    "provider_status",
    "split_list",
]


def any_provider_configured() -> bool:
    """Whether this deployment can reach a model at all, through any vendor.

    The question every route asks before spending a budget, and the one the extraction
    enqueue asks before queueing work whose only possible outcome would be failure. It is
    a chain-wide question by construction: with three interchangeable providers, "can we
    answer" cannot be a statement about any single vendor's key.
    """
    return bool(build_chain())
