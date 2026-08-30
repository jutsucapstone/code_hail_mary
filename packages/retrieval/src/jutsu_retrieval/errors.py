"""Embedding failures, classified by what you should do about them.

**The classification is the point.** An embedding client that retries everything turns a
permanently malformed input into an infinite loop against a metered API, and one that
retries nothing gives up on a per-minute quota that would have cleared in twenty seconds.
Both were observed live on 2026-08-27: a 429 on
`online_prediction_requests_per_base_model` after roughly eight rapid requests, and a 400
`INVALID_ARGUMENT` for an unknown `task_type` and for empty content.

So there are exactly two retryable-or-not classes, and everything the client raises is
one of them.

`TruncatedInput` is the odd one and the most dangerous. It is **not an HTTP error** — the
provider returned 200 with a perfectly well-formed vector — but the vector represents
only the first 2048 tokens of the input. Nothing about the response says the answer is
wrong. Treating it as success is how a corpus acquires embeddings that quietly do not
match their chunks.
"""

from __future__ import annotations

__all__ = [
    "EmbeddingBudgetExceeded",
    "EmbeddingError",
    "PermanentEmbeddingError",
    "TransientEmbeddingError",
    "TruncatedInput",
]


class EmbeddingError(RuntimeError):
    """Base for everything this package raises."""


class TransientEmbeddingError(EmbeddingError):
    """Worth retrying: 429, 5xx, a timeout, or a connection failure.

    Carries `status` where there was one, so a caller can log the class of failure
    without logging the response body.

    `retry_after` is the provider's own instruction, in seconds, taken from the
    `Retry-After` header when it sends one. It exists because guessing is measurably
    worse: the 200-document drain issued 551 requests and 405 of them were rejected —
    a 73% rejection rate — because every retry picked its own jittered delay while the
    provider was saying exactly how long to wait. `None` means the response carried no
    usable header and the caller should fall back to backoff.
    """

    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class PermanentEmbeddingError(EmbeddingError):
    """Not worth retrying: 4xx other than 429.

    A 400 means the request will be rejected identically every time — an unknown
    `task_type`, empty content, a malformed parameter. Retrying it spends quota to
    receive the same answer, and on a corpus-sized job it spends a great deal of it.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TruncatedInput(EmbeddingError):
    """The provider silently truncated the input and embedded only part of it.

    Measured: 2081 reported tokens came back `truncated=True` under HTTP 200. The vector
    is real, well-formed and wrong — it describes a prefix of the chunk.

    Raised rather than tolerated, and never persisted. The repair is to re-chunk the
    document at a lower target, which is the caller's decision because it changes stored
    offsets; this layer refuses and says which index failed.
    """

    def __init__(self, message: str, *, index: int, token_count: int) -> None:
        super().__init__(message)
        self.index = index
        self.token_count = token_count


class EmbeddingBudgetExceeded(EmbeddingError):
    """The job's token ceiling was reached.

    §20 asks for cost guardrails on day one rather than after the first bill. Budget
    alerts are the backstop in the billing console; this is the one in the code, and it
    is what stops a retry loop from re-embedding a corpus at full price.
    """

    def __init__(self, message: str, *, spent: int, budget: int) -> None:
        super().__init__(message)
        self.spent = spent
        self.budget = budget
