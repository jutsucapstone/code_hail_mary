"""A cross-instance budget for the endpoint that spends money on demand (§20).

`POST /v1/search` embeds the caller's question before it can search. That is a paid Vertex
request per call, reachable by every authenticated employee, and nothing bounded how many.
The per-request `TokenLedger` bounds *one* request; this bounds how many a caller gets.

**It commits in its own transaction, and that is the whole design.**

`deps.get_db` wraps a request in `session.begin()`, so the entire request rolls back on
any exception. A counter incremented on that session would therefore be *undone by every
failure* — and the failures that matter here are the expensive ones. A provider 503 would
roll back the spend, the caller retries, the spend rolls back again: an unbounded loop
against a metered API, produced by a rate limiter that appears to be working. So the
spend is taken on a separate session, committed before the embedding call is made, and it
survives whatever the request does next.

The consequence is deliberate and is the policy: **a failed search still consumes quota.**
The quota counts attempts, because an attempt is what costs money and what a retry loop
repeats. Counting only successes would mean a caller whose requests all fail has no limit
at all.

**Fixed window, not a token bucket.** A fixed window admits a burst at a boundary — up to
`2x the limit` across two adjacent windows — and that is acceptable here: the limit exists to
stop a runaway, not to smooth traffic. It is also one atomic statement, where a bucket
needs a stored timestamp and a rate computation, and `auth.spend_registration_budget`
already set this precedent in migration 0007.
"""

from __future__ import annotations

import os
from typing import Final
from uuid import UUID

from jutsu_core.errors import RateLimited
from jutsu_db.engine import org_session
from sqlalchemy import text

__all__ = [
    "DEFAULT_SEARCH_RATE_LIMIT",
    "DEFAULT_SEARCH_RATE_WINDOW_S",
    "SearchRateLimitSettings",
    "search_rate_limit_settings",
    "spend_search_budget",
]

DEFAULT_SEARCH_RATE_LIMIT: Final = 60
DEFAULT_SEARCH_RATE_WINDOW_S: Final = 60

_LIMIT_ENV: Final = "SEARCH_RATE_LIMIT"
_WINDOW_ENV: Final = "SEARCH_RATE_WINDOW_S"

#: One atomic statement: read, roll the window if it has elapsed, increment, and report
#: what remains. Split into a SELECT and an UPDATE, concurrent requests each read the old
#: value and each conclude they are under the limit — the exact failure a limiter exists
#: to prevent. Taken in shape from `auth.spend_registration_budget`.
#:
#: `RETURNING :limit - spent` is negative-or-zero exactly when this caller has spent their
#: allowance, and the row is written either way: a refused caller does not get a free
#: retry by being refused.
_SPEND: Final = """
INSERT INTO search_budget (org_id, user_id, window_start, spent)
VALUES (
    NULLIF(current_setting('app.current_org_id', true), '')::uuid,
    CAST(:user_id AS uuid),
    now(),
    1
)
ON CONFLICT (org_id, user_id) DO UPDATE
   SET window_start = CASE
         WHEN search_budget.window_start < now() - make_interval(secs => CAST(:window AS integer))
         THEN now() ELSE search_budget.window_start END,
       spent = CASE
         WHEN search_budget.window_start < now() - make_interval(secs => CAST(:window AS integer))
         THEN 1 ELSE search_budget.spent + 1 END
RETURNING CAST(:limit AS integer) - spent
"""


class SearchRateLimitSettings:
    """How many searches, over how long. Read from the environment, validated once."""

    __slots__ = ("limit", "window_seconds")

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds


def _positive(name: str, default: int) -> int:
    """An unset variable means the default; a zero or negative one is a mistake.

    Reading `0` as "unlimited" is how a guardrail disappears without anybody removing it
    — the same reasoning `EMBEDDING_TOKEN_BUDGET=0` is refused for. A misconfiguration
    fails the request loudly rather than silently removing the ceiling.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer, not {raw!r}.") from error
    if value < 1:
        raise RuntimeError(f"{name}={value} is not a limit. Unset it to use the default.")
    return value


def search_rate_limit_settings() -> SearchRateLimitSettings:
    """Read per call rather than cached, so a deployment can change the limit.

    Cheap — two environment reads — and the alternative is a process that has to be
    restarted to widen a limit during the incident that made you want to widen it.
    """
    return SearchRateLimitSettings(
        limit=_positive(_LIMIT_ENV, DEFAULT_SEARCH_RATE_LIMIT),
        window_seconds=_positive(_WINDOW_ENV, DEFAULT_SEARCH_RATE_WINDOW_S),
    )


async def spend_search_budget(
    *, org_id: UUID, user_id: UUID, settings: SearchRateLimitSettings | None = None
) -> int:
    """Charge one search to this caller. Raises `RateLimited` when there is none left.

    Opens its own `org_session`, so the spend commits independently of the request
    transaction — see the module docstring. The organisation comes from the authenticated
    `Principal`, never from anything the browser sent, and the session GUC it sets is what
    the row-level policy compares against, so a caller can only ever spend their own
    tenant's budget.

    Returns the remaining allowance, for the caller to log or surface.
    """
    config = settings if settings is not None else search_rate_limit_settings()

    async with org_session(org_id) as session:
        remaining = (
            await session.execute(
                text(_SPEND),
                {
                    "user_id": str(user_id),
                    "window": config.window_seconds,
                    "limit": config.limit,
                },
            )
        ).scalar_one()

    if remaining < 0:
        # Never the question, never the user id, never the organisation — a limit
        # message is read by whoever is being limited and by whatever logs the response
        # (§4.9). The numbers here are configuration, not data.
        raise RateLimited(
            "Too many searches. Try again shortly.",
            details={"limit": config.limit, "window_seconds": config.window_seconds},
        )
    return int(remaining)
