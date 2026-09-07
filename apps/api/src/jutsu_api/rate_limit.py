"""Cross-instance budgets for the endpoints that spend money or invite probing (§20).

`POST /v1/search` embeds the caller's question before it can search — a paid request per
call, reachable by every authenticated employee, and nothing bounded how many. That was
the first budget. Two more joined it with the KT console: `POST /v1/kt/claim` is a 40-bit
code space whose only defence was an audit trail of refused guesses, and the handover
summary is a paid model call with no ceiling at all.

**It commits in its own transaction, and that is the whole design.**

`deps.get_db` wraps a request in `session.begin()`, so the entire request rolls back on
any exception. A counter incremented on that session would therefore be *undone by every
failure* — and the failures that matter here are the expensive ones. A provider 503 would
roll back the spend, the caller retries, the spend rolls back again: an unbounded loop
against a metered API, produced by a rate limiter that appears to be working. So the
spend is taken on a separate session, committed before the guarded step, and it survives
whatever the request does next.

The consequence is deliberate and is the policy: **a refused or failed attempt still
consumes quota.** The quota counts attempts, because an attempt is what costs money — or,
for the claim endpoint, what a probe repeats. Counting only successes would mean a caller
whose requests all fail has no limit at all.

**One table, many buckets.** Migration 0019 added `bucket` to the key of `search_budget`
rather than creating a sibling table per endpoint: two limiters is how one stops being
maintained, and one atomic statement is one place the concurrency argument has to hold.
The table keeps its historical name; every budget in this module lives in it.

**Fixed window, not a token bucket.** A fixed window admits a burst at a boundary — up to
`2x the limit` across two adjacent windows — and that is acceptable here: the limit exists to
stop a runaway, not to smooth traffic. It is also one atomic statement, where a bucket
needs a stored timestamp and a rate computation, and `auth.spend_registration_budget`
already set this precedent in migration 0007.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from jutsu_core.errors import RateLimited
from jutsu_db.engine import org_session
from sqlalchemy import text

__all__ = [
    "DEFAULT_KT_CLAIM_RATE_LIMIT",
    "DEFAULT_KT_CLAIM_RATE_WINDOW_S",
    "DEFAULT_KT_OPEN_RATE_LIMIT",
    "DEFAULT_KT_OPEN_RATE_WINDOW_S",
    "DEFAULT_KT_SUMMARY_RATE_LIMIT",
    "DEFAULT_KT_SUMMARY_RATE_WINDOW_S",
    "DEFAULT_SEARCH_RATE_LIMIT",
    "DEFAULT_SEARCH_RATE_WINDOW_S",
    "Bucket",
    "BudgetSettings",
    "SearchRateLimitSettings",
    "budget_settings",
    "search_rate_limit_settings",
    "spend_budget",
    "spend_search_budget",
]

DEFAULT_SEARCH_RATE_LIMIT: Final = 60
DEFAULT_SEARCH_RATE_WINDOW_S: Final = 60

#: A person types a KT ID once, perhaps twice after a typo. Ten a minute is far above
#: honest use and turns a 2^40 code space into something no probe finishes; the denied
#: opens the trail already records become the evidence, and this becomes the wall.
DEFAULT_KT_CLAIM_RATE_LIMIT: Final = 10
DEFAULT_KT_CLAIM_RATE_WINDOW_S: Final = 60

#: Opening a package by code on any route other than the claim door.
#:
#: `KT_CLAIM` walls `POST /v1/kt/claim` at ten a minute, but a dozen sibling routes
#: take a caller-supplied code and reach the same lookup — so guessing against
#: `GET /v1/kt/{code}/documents` was free while the front door was walled. It cannot
#: share the claim bucket: a person reading a KT package makes several requests per
#: panel and would spend ten in seconds.
#:
#: A hundred and twenty a minute is far above what a session does — the console
#: mounts a handful of queries per tab — and far below what a probe needs against a
#: 32^8 code space. It is charged BEFORE the lookup, so a hit and a miss cost the
#: same: a budget spent only on misses would tell a prober which guesses were close.
DEFAULT_KT_OPEN_RATE_LIMIT: Final = 120
DEFAULT_KT_OPEN_RATE_WINDOW_S: Final = 60

#: The handover summary is one paid model call per press, composed fresh and never
#: cached. Six a minute lets somebody retry after a refusal and stops a held-down key.
DEFAULT_KT_SUMMARY_RATE_LIMIT: Final = 6
DEFAULT_KT_SUMMARY_RATE_WINDOW_S: Final = 60


class Bucket(StrEnum):
    """Which budget a spend charges. The value is the `bucket` column."""

    SEARCH = "search"
    KT_CLAIM = "kt_claim"
    KT_OPEN = "kt_open"
    KT_SUMMARY = "kt_summary"


@dataclass(frozen=True, slots=True)
class _BudgetSpec:
    limit_env: str
    window_env: str
    default_limit: int
    default_window: int
    #: Read by whoever is being limited and by whatever logs the response — so never the
    #: question, never an id (§4.9). The sentence names the action, nothing else.
    refusal: str


_SPECS: Final[dict[Bucket, _BudgetSpec]] = {
    Bucket.SEARCH: _BudgetSpec(
        limit_env="SEARCH_RATE_LIMIT",
        window_env="SEARCH_RATE_WINDOW_S",
        default_limit=DEFAULT_SEARCH_RATE_LIMIT,
        default_window=DEFAULT_SEARCH_RATE_WINDOW_S,
        refusal="Too many searches. Try again shortly.",
    ),
    Bucket.KT_CLAIM: _BudgetSpec(
        limit_env="KT_CLAIM_RATE_LIMIT",
        window_env="KT_CLAIM_RATE_WINDOW_S",
        default_limit=DEFAULT_KT_CLAIM_RATE_LIMIT,
        default_window=DEFAULT_KT_CLAIM_RATE_WINDOW_S,
        refusal="Too many attempts to open a package. Try again shortly.",
    ),
    Bucket.KT_OPEN: _BudgetSpec(
        limit_env="KT_OPEN_RATE_LIMIT",
        window_env="KT_OPEN_RATE_WINDOW_S",
        default_limit=DEFAULT_KT_OPEN_RATE_LIMIT,
        default_window=DEFAULT_KT_OPEN_RATE_WINDOW_S,
        refusal="Too many requests for this package. Try again shortly.",
    ),
    Bucket.KT_SUMMARY: _BudgetSpec(
        limit_env="KT_SUMMARY_RATE_LIMIT",
        window_env="KT_SUMMARY_RATE_WINDOW_S",
        default_limit=DEFAULT_KT_SUMMARY_RATE_LIMIT,
        default_window=DEFAULT_KT_SUMMARY_RATE_WINDOW_S,
        refusal="Too many summaries composed. Try again shortly.",
    ),
}

#: One atomic statement: read, roll the window if it has elapsed, increment, and report
#: what remains. Split into a SELECT and an UPDATE, concurrent requests each read the old
#: value and each conclude they are under the limit — the exact failure a limiter exists
#: to prevent. Taken in shape from `auth.spend_registration_budget`.
#:
#: `RETURNING :limit - spent` is negative-or-zero exactly when this caller has spent their
#: allowance, and the row is written either way: a refused caller does not get a free
#: retry by being refused.
_SPEND: Final = """
INSERT INTO search_budget (org_id, user_id, bucket, window_start, spent)
VALUES (
    NULLIF(current_setting('app.current_org_id', true), '')::uuid,
    CAST(:user_id AS uuid),
    :bucket,
    now(),
    1
)
ON CONFLICT (org_id, user_id, bucket) DO UPDATE
   SET window_start = CASE
         WHEN search_budget.window_start < now() - make_interval(secs => CAST(:window AS integer))
         THEN now() ELSE search_budget.window_start END,
       spent = CASE
         WHEN search_budget.window_start < now() - make_interval(secs => CAST(:window AS integer))
         THEN 1 ELSE search_budget.spent + 1 END
RETURNING CAST(:limit AS integer) - spent
"""


class BudgetSettings:
    """How many attempts, over how long. Read from the environment, validated once."""

    __slots__ = ("limit", "window_seconds")

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds


#: The name the search limiter shipped under. Kept so nothing that imported it moves.
SearchRateLimitSettings = BudgetSettings


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


def budget_settings(bucket: Bucket) -> BudgetSettings:
    """Read per call rather than cached, so a deployment can change the limit.

    Cheap — two environment reads — and the alternative is a process that has to be
    restarted to widen a limit during the incident that made you want to widen it.
    """
    spec = _SPECS[bucket]
    return BudgetSettings(
        limit=_positive(spec.limit_env, spec.default_limit),
        window_seconds=_positive(spec.window_env, spec.default_window),
    )


def search_rate_limit_settings() -> BudgetSettings:
    return budget_settings(Bucket.SEARCH)


async def spend_budget(
    bucket: Bucket, *, org_id: UUID, user_id: UUID, settings: BudgetSettings | None = None
) -> int:
    """Charge one attempt in `bucket` to this caller. Raises `RateLimited` when none is left.

    Opens its own `org_session`, so the spend commits independently of the request
    transaction — see the module docstring. The organisation comes from the authenticated
    `Principal`, never from anything the browser sent, and the session GUC it sets is what
    the row-level policy compares against, so a caller can only ever spend their own
    tenant's budget.

    Returns the remaining allowance, for the caller to log or surface.
    """
    spec = _SPECS[bucket]
    config = settings if settings is not None else budget_settings(bucket)

    async with org_session(org_id) as session:
        remaining = (
            await session.execute(
                text(_SPEND),
                {
                    "user_id": str(user_id),
                    "bucket": bucket.value,
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
            spec.refusal,
            details={"limit": config.limit, "window_seconds": config.window_seconds},
        )
    return int(remaining)


async def spend_search_budget(
    *, org_id: UUID, user_id: UUID, settings: BudgetSettings | None = None
) -> int:
    """Charge one search. The original entry point; `/v1/search` and `/v1/ask` call it."""
    return await spend_budget(Bucket.SEARCH, org_id=org_id, user_id=user_id, settings=settings)
