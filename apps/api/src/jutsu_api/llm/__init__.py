"""Multi-provider answer generation: Claude first, three fallbacks behind it (ADR 0023).

This package is an availability layer and nothing else. It sits inside the
`AnswerTransport` seam `answers.py` has always called, which means every question about
*what* the model is asked — the system prompt, the retrieved passages, the numbered
evidence, the conversation preamble — is settled before anything here runs, and every
question about whether the answer may be shown — the citation gate, the retry, the refusal
— is settled after it returns.

    types      the request, the response, and the failure taxonomy
    providers  one adapter per vendor: authenticate, shape, parse, classify
    failover   the ordered chain, its budget, and what it logs

Nothing in this package sees a tenant, a principal, a document or a chunk.
"""

from jutsu_api.llm.failover import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_ORDER,
    DEFAULT_PROVIDER_TIMEOUT_S,
    DEFAULT_TOTAL_TIMEOUT_S,
    FailoverTransport,
    build_chain,
    configured_order,
    provider_status,
)
from jutsu_api.llm.providers import (
    CerebrasProvider,
    ClaudeProvider,
    GroqProvider,
    OpenAICompatibleProvider,
    OpenRouterProvider,
    build_provider,
)
from jutsu_api.llm.types import (
    DEFAULT_MAX_TOKENS,
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
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_ORDER",
    "DEFAULT_PROVIDER_TIMEOUT_S",
    "DEFAULT_TOTAL_TIMEOUT_S",
    "CerebrasProvider",
    "ClaudeProvider",
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
]


def any_provider_configured() -> bool:
    """Whether this deployment can answer at all, through any vendor.

    The question `answers_configured()` asks on behalf of every route that refuses for
    free before spending a budget. It became a chain-wide question the moment Claude
    stopped being the only provider: a deployment holding a Groq key and no Anthropic key
    can answer perfectly well, and gating that on the primary's key would refuse every
    question while a working provider sat configured and idle.
    """
    return bool(build_chain())
