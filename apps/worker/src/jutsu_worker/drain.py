"""One drain, and what to do about what it leaves behind. Shared by both transports.

Two kinds of leftover, two follow-ups. Work claimable NOW remains when the drain stopped
at its own soft deadline (or a predecessor was killed at the hard one) — ring again almost
immediately. A `retry_scheduled` job with a future `next_attempt_at` has no doorbell of
its own — the doorbell fires on *enqueue*, and a retry is not an enqueue — so ring after
the shortest backoff has passed.

The decision lives here rather than in either dispatcher because both must make it the
same way: the arq handler (dev) rings Redis, the HTTP door (prod) rings Cloud Tasks, and a
drain that forgot to re-ring on one transport would look like a queue that stalls only in
production. A test asserts both call this function.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Literal

from jutsu_db.engine import org_session
from sqlalchemy import text

from jutsu_worker.runner import drain_org

__all__ = [
    "FOLLOW_UP_NOW_SECONDS",
    "FOLLOW_UP_RETRY_SECONDS",
    "DrainReport",
    "FollowUp",
    "drain_and_report",
    "follow_up_delay",
]

logger = logging.getLogger("jutsu.worker.drain")

#: Work is claimable now: the drain stopped short, so ring again almost at once.
FOLLOW_UP_NOW_SECONDS = 5
#: Only retries with a future `next_attempt_at` remain: ring after the shortest backoff.
FOLLOW_UP_RETRY_SECONDS = 60

FollowUp = Literal["now", "retry"] | None


@dataclass(frozen=True)
class DrainReport:
    counts: dict[str, int]
    claimable_now: int
    retries_waiting: int
    follow_up: FollowUp


def follow_up_delay(follow_up: FollowUp) -> int | None:
    if follow_up == "now":
        return FOLLOW_UP_NOW_SECONDS
    if follow_up == "retry":
        return FOLLOW_UP_RETRY_SECONDS
    return None


async def drain_and_report(org_id: uuid.UUID) -> DrainReport:
    """Drain one organisation's queue, then say whether it needs ringing again.

    The org id is a hint about where to look, never an authorization: every query the
    drain runs is scoped by row-level security to exactly that organisation (ADR 0012).
    """
    counts = await drain_org(org_id)

    async with org_session(org_id) as session:
        claimable_now = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM jobs WHERE state = 'pending' "
                        "OR (state = 'retry_scheduled' AND next_attempt_at <= now())"
                    )
                )
            ).scalar_one()
        )
        retries_waiting = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM jobs WHERE state = 'retry_scheduled' "
                        "AND next_attempt_at > now()"
                    )
                )
            ).scalar_one()
        )

    # Progress is what makes an immediate follow-up worth anything. `claimable_now`
    # counts rows in a claimable *state*, which is not the same as work this drain can
    # do: `drain_org` skips embedding entirely when no provider is configured, so an
    # organisation holding pending `embed.document` rows with no Vertex would ring
    # itself every five seconds, for ever, draining nothing each time. A drain that
    # claimed nothing will claim nothing again five seconds later; recovery there is
    # the next real doorbell — a sign-in, the Jobs page, or the next enqueue
    # (ADR 0017 §5) — not a hot loop against an unchanged table.
    progressed = sum(counts.values()) > 0
    follow_up: FollowUp = None
    if claimable_now and progressed:
        follow_up = "now"
    elif retries_waiting:
        follow_up = "retry"
    logger.info(
        "%s",
        {
            "event": "org_drained",
            "org_id": str(org_id),
            "jobs": sum(counts.values()),
            "counts": counts,
            "claimable_now": claimable_now,
            "retries_waiting": retries_waiting,
            "progressed": progressed,
            "follow_up": follow_up,
        },
    )
    return DrainReport(
        counts=counts,
        claimable_now=claimable_now,
        retries_waiting=retries_waiting,
        follow_up=follow_up,
    )
