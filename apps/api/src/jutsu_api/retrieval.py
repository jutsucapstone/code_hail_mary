"""Turning a question into a vector, and a position into a cursor (§12, §13).

Two things the search route needs that no existing module provides, kept out of the
router so `routers/search.py` stays as thin as `routers/evidence.py`.

**The budget lifecycle is the whole reason this file exists.** `TokenLedger` is a
*ceiling with memory*: it accumulates `spent` and refuses once the budget is gone. That
is exactly right for `make seed`, a process that runs a corpus and exits. In a
long-lived API process the same object would accumulate across every request ever
served, cross a fixed ceiling some afternoon, and then refuse **every** subsequent
search for the lifetime of the process — a total outage produced by a guardrail working
as designed in the wrong scope. So the ledger is built per request and discarded with
it, and the transport, which owns credentials and an HTTP connection pool, is not.

**`Embedder` is also per request, and that costs something.** Its `_not_before` cooldown
is shared between the batches of one embedder, so building one per request means two
concurrent searches do not share a rate-limit cooldown. That is a deliberate trade: a
shared embedder would mean a shared ledger, which is the outage above. A search sends
exactly one instance of a few dozen tokens, so the storm the cooldown was written for —
250-instance document batches, back to back — is not the shape of this endpoint's
traffic. Cross-request pacing belongs in the rate limiter, which is a separate concern
and currently a reported gap rather than an implemented one.

Nothing here logs the query. It is user-authored text and may contain anything §4.9
forbids in a log line.
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import Final
from uuid import UUID

from jutsu_core.errors import RateLimited, ServiceUnavailable, ValidationFailed
from jutsu_retrieval import (
    Embedder,
    EmbeddingBudgetExceeded,
    EmbeddingTask,
    EmbeddingTransport,
    MissingEmbeddingSettings,
    PermanentEmbeddingError,
    TokenLedger,
    TransientEmbeddingError,
    TruncatedInput,
    VertexTransport,
    get_embedding_settings,
)

__all__ = [
    "MAX_QUERY_CHARS",
    "QueryEmbedder",
    "decode_cursor",
    "encode_cursor",
    "get_query_embedder",
    "reset_query_embedder",
]

#: Longest question this endpoint will embed.
#:
#: The provider truncates silently over its input limit and returns HTTP 200 with a
#: vector describing only a prefix — the trap `TruncatedInput` exists for. A search that
#: quietly answered a prefix of the question would be wrong in the least visible way
#: available, so the length is refused here, in validation, where the caller is told.
#: Four thousand characters is far below the 2048-token limit on any script in the
#: corpus and longer than any real question.
MAX_QUERY_CHARS: Final = 4_000

#: Per-request embedding ceiling, from §13's `TOKEN_BUDGET_PER_REQUEST`.
#:
#: Read here and **never** into `EmbeddingSettings`: CLAUDE.md records that wiring this
#: variable into the embedding settings would replace the corpus job's
#: `EMBEDDING_TOKEN_BUDGET` and stop a 45k-document seed after a few dozen documents.
#: This is the API's per-request bound and touches nothing the worker reads.
#:
#: For search alone the value is enormous — one question is a few dozen tokens against a
#: default of 120,000. That is not a mistake to correct by inventing a tighter number
#: here: the variable is §13's bound on *one agent question*, and search is the first of
#: several calls that will eventually sit under it. It bounds a runaway, not a query.
_BUDGET_ENV: Final = "TOKEN_BUDGET_PER_REQUEST"
_DEFAULT_BUDGET: Final = 120_000


def _per_request_budget() -> int:
    raw = os.environ.get(_BUDGET_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_BUDGET
    try:
        value = int(raw)
    except ValueError as error:
        raise MissingEmbeddingSettings(
            f"{_BUDGET_ENV} must be an integer number of tokens, not {raw!r}."
        ) from error
    if value < 1:
        # Same reasoning as `EMBEDDING_TOKEN_BUDGET=0`: unset means "use the default",
        # zero means somebody typed a ceiling wrong, and reading it as "unlimited" is
        # how a guardrail disappears without anyone removing it.
        raise MissingEmbeddingSettings(f"{_BUDGET_ENV}={value} is not a ceiling.")
    return value


class QueryEmbedder:
    """One `RETRIEVAL_QUERY` vector per call, on a fresh ledger each time.

    Holds the transport — credentials and an httpx pool, both expensive and both safe to
    share — and builds the `Embedder` and its `TokenLedger` per request.
    """

    __slots__ = ("_settings", "_transport")

    def __init__(self, transport: EmbeddingTransport, settings: object | None = None) -> None:
        self._transport = transport
        self._settings = settings if settings is not None else get_embedding_settings()

    async def embed(self, query: str) -> tuple[list[float], int]:
        """Return `(vector, tokens_charged)`.

        `RETRIEVAL_QUERY`, never `RETRIEVAL_DOCUMENT`. Measured on identical text the two
        embeddings sit at cosine 0.915 — close enough to look like it works and far
        enough to cost several points of recall, which is invisible until eval.
        """
        embedder = Embedder(
            self._transport,
            self._settings,  # type: ignore[arg-type]
            ledger=TokenLedger(budget=_per_request_budget()),
        )
        try:
            embeddings = await embedder.embed([query], EmbeddingTask.QUERY)
        except EmbeddingBudgetExceeded as error:
            # 429 rather than 500: the request was well formed and the service is
            # healthy; a ceiling this caller shares was reached. `Retry-After` is not set
            # because the budget is per request and the next one starts fresh.
            raise RateLimited(
                "This request reached its embedding token budget.",
                details={"budget": error.budget},
            ) from error
        except PermanentEmbeddingError as error:
            # 422, not 502. A permanent embedding error means the *input* was rejected
            # and will be rejected identically for ever; calling it a provider fault
            # invites a retry that cannot succeed.
            raise ValidationFailed("The query could not be embedded.") from error
        except TruncatedInput as error:
            raise ValidationFailed(
                "The query is too long to embed without truncation.",
                details={"max_chars": MAX_QUERY_CHARS},
            ) from error
        except TransientEmbeddingError as error:
            # 503. The provider is rate-limiting or having a moment, the request was
            # fine, and trying again later is the correct advice.
            raise ServiceUnavailable(
                "The embedding provider is unavailable. Try again shortly."
            ) from error

        return list(embeddings[0].vector), embeddings[0].token_count


_embedder: QueryEmbedder | None = None


def get_query_embedder() -> QueryEmbedder:
    """Process-wide embedder, built on first use.

    Lazy rather than at import: `create_app()` runs in `scripts/emit-openapi.py` with no
    credentials and no project, and a schema dump must not need a Vertex configuration.

    Missing configuration is translated to the 503 the envelope can carry. It used to
    escape as a bare RuntimeError — a 500 with "something went wrong on our side" — which
    told a caller on an unconfigured deployment nothing at all, and told the §34 states
    story exactly wrong: this is a deployment fact, not a fault.
    """
    global _embedder
    if _embedder is None:
        try:
            _embedder = QueryEmbedder(VertexTransport(get_embedding_settings()))
        except MissingEmbeddingSettings as exc:
            raise ServiceUnavailable(
                "Search is not configured for this deployment yet: the embedding "
                "provider needs credentials before queries can be embedded."
            ) from exc
    return _embedder


def reset_query_embedder() -> None:
    """Drop the cached embedder. For tests and for a settings change in development."""
    global _embedder
    _embedder = None


# ------------------------------------------------------------------------------- cursor


def encode_cursor(score: float, chunk_id: UUID) -> str:
    """`(score, chunk_id)` as one opaque token.

    `repr` on the float rather than a rounded format: the value goes back into the
    keyset comparison, and a cursor that rounds is a cursor that can skip or repeat the
    row it names. `repr` round-trips a Python float exactly.
    """
    return base64.urlsafe_b64encode(f"{score!r}:{chunk_id}".encode()).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> tuple[float, UUID]:
    """Parse a cursor, or raise `ValidationFailed`.

    **Unsigned, deliberately.** A cursor carries no authorization: the next page
    re-resolves the caller's principals inside the query and re-applies `ACL_PREDICATE`
    unchanged, so the worst an edited cursor can do is name a position in an ordering the
    caller is already entitled to read. Signing it would add a key to rotate and protect
    nothing — and would imply to the next reader that the cursor *is* trusted, which is
    the belief that makes someone put a filter in it later.

    Strict about shape for the ordinary reason: a malformed cursor is a 422 the caller
    can act on, not a 500 and not a silently ignored first page.
    """
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise ValidationFailed("cursor is not a valid pagination token") from error

    score_text, separator, id_text = raw.partition(":")
    if not separator:
        raise ValidationFailed("cursor is not a valid pagination token")
    try:
        score = float(score_text)
        chunk_id = UUID(id_text)
    except ValueError as error:
        raise ValidationFailed("cursor is not a valid pagination token") from error
    # NaN compares false against everything, so a cursor carrying one would silently
    # return an empty page for ever rather than failing.
    if score != score:
        raise ValidationFailed("cursor is not a valid pagination token")
    return score, chunk_id
