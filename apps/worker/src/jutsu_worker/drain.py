"""One drain, and what to do about what it leaves behind. Shared by both transports.

Three kinds of leftover, three follow-ups. Work claimable NOW remains when the drain
stopped at its own soft deadline (or a predecessor was killed at the hard one) — ring
again almost immediately. A `retry_scheduled` job with a future `next_attempt_at` has no
doorbell of its own — the doorbell fires on *enqueue*, and a retry is not an enqueue —
so ring after the shortest backoff has passed. And a job a killed worker left in a
WORKING state is claimable by nobody until its lease expires: it is counted by neither
of the first two, so without the third the row waits for an unrelated doorbell that may
never come. Ring just after the earliest lease expires, and the sweep at the top of the
next drain reclaims it.

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

from jutsu_worker.jobs import WORKING
from jutsu_worker.runner import drain_org

__all__ = [
    "FOLLOW_UP_LEASE_FLOOR_SECONDS",
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
#: Never ring for an expiring lease sooner than this, however close the expiry looks —
#: clocks differ by a little and a ring that lands before the lease lapses claims nothing.
FOLLOW_UP_LEASE_FLOOR_SECONDS = 5
#: And never sit on one longer than this, so a clock far in the future cannot park an
#: organisation's queue for a day.
FOLLOW_UP_LEASE_CEILING_SECONDS = 3600

FollowUp = Literal["now", "retry", "lease"] | None


@dataclass(frozen=True)
class DrainReport:
    counts: dict[str, int]
    claimable_now: int
    retries_waiting: int
    follow_up: FollowUp
    #: Rows a worker holds right now whose lease has not expired — a crash leaves these.
    leases_held: int = 0
    #: When to ring for the earliest of those, in seconds from now. Dynamic, so it is
    #: carried rather than derived from the label like the other two.
    lease_seconds: int | None = None

    @property
    def follow_up_seconds(self) -> int | None:
        """How long to wait before ringing again, or None for no follow-up."""
        if self.follow_up == "lease":
            return self.lease_seconds
        return follow_up_delay(self.follow_up)


def follow_up_delay(follow_up: FollowUp) -> int | None:
    """The fixed delay for a labelled follow-up. `lease` is dynamic and lives on the
    report; prefer `DrainReport.follow_up_seconds`, which handles all three."""
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
        # A worker killed mid-job leaves its row in a working state holding a lease
        # nobody else may take until it lapses. `EXTRACT(EPOCH FROM …)` rather than the
        # interval, so the wait is arithmetic here and not a timezone question.
        held = (
            await session.execute(
                text(
                    "SELECT count(*), "
                    "COALESCE(CEIL(EXTRACT(EPOCH FROM (min(locked_until) - now()))), 0) "
                    "FROM jobs WHERE state = ANY(:working) AND locked_until > now()"
                ),
                {"working": [state.value for state in WORKING]},
            )
        ).one()
        leases_held = int(held[0])
        lease_seconds = (
            min(
                max(int(held[1]) + 1, FOLLOW_UP_LEASE_FLOOR_SECONDS),
                FOLLOW_UP_LEASE_CEILING_SECONDS,
            )
            if leases_held
            else None
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
    elif leases_held:
        # Nothing claimable and nothing scheduled, but somebody's lease is still running.
        # Either it finishes (and its own drain reports what is left) or it lapses and
        # this ring finds it reclaimed.
        follow_up = "lease"
    logger.info(
        "%s",
        {
            "event": "org_drained",
            "org_id": str(org_id),
            "jobs": sum(counts.values()),
            "counts": counts,
            "claimable_now": claimable_now,
            "retries_waiting": retries_waiting,
            "leases_held": leases_held,
            "progressed": progressed,
            "follow_up": follow_up,
        },
    )
    return DrainReport(
        counts=counts,
        claimable_now=claimable_now,
        retries_waiting=retries_waiting,
        follow_up=follow_up,
        leases_held=leases_held,
        lease_seconds=lease_seconds,
    )
