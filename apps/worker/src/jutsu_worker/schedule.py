"""The clock: enqueue a sync for every organisation whose local hour has arrived.

Run once and exit, as a scheduled Cloud Run job — the same shape as the reaper, for the
same reason (ADR 0017 §3): a scheduler that lives inside a process means a container that
never idles. Cloud Scheduler ticks this every fifteen minutes; `sched.due_organisations`
decides who is actually due, in their own timezone, at most once per local day.

**This job cannot read a tenant.** It learns which organisations exist from
`sched.due_organisations`, which returns an id, a timezone and an hour and nothing else
(ADR 0018), and then does all its work inside an ordinary `org_session` with row-level
security on and no bypass anywhere. It holds no provider credential and fetches nothing:
it writes `connector.sync` rows with the same idempotency key "Sync now" uses and rings
ADR 0017's doorbell. Everything after that is the pipeline that already existed.

The consequence of that key being shared is the property worth having: a nightly run and
a person pressing "Sync now" a second earlier cannot produce two walks of the same
connection.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from jutsu_core.doorbell import CloudTasksDoorbell, MisconfiguredDoorbell
from jutsu_core.logs import configure as configure_logging
from jutsu_db import unscoped_session
from jutsu_db.engine import org_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["DueOrganisation", "SyncRun", "main", "run_due_syncs", "sync_organisation"]

logger = logging.getLogger("jutsu.worker.schedule")

#: States a connection must be in to be worth syncing. `error` is included for the same
#: reason `sync_now` includes it: last night's failure is exactly what tonight should
#: retry, and a connection that needs reauthentication is `reauth_required`, not `error`.
SYNCABLE_STATES = ("connected", "error")


@dataclass(frozen=True, slots=True)
class DueOrganisation:
    org_id: uuid.UUID
    timezone: str
    hour_local: int


@dataclass(frozen=True, slots=True)
class SyncRun:
    org_id: uuid.UUID
    connections: int
    enqueued: int
    outcome: str
    rung: bool


async def due_organisations(session: AsyncSession, *, now: datetime) -> list[DueOrganisation]:
    """Who is due, in their own zone. The one cross-tenant read in the system."""
    rows = (
        await session.execute(
            text("SELECT org_id, timezone, hour_local FROM sched.due_organisations(:now)"),
            {"now": now},
        )
    ).all()
    return [
        DueOrganisation(org_id=uuid.UUID(str(r[0])), timezone=str(r[1]), hour_local=int(r[2]))
        for r in rows
    ]


async def _claim(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Mark the run started, and say whether this caller is the one that started it.

    Claimed before the work, like every other claim here: the tick runs every fifteen
    minutes and an organisation is due for a whole hour, so without this a slow run would
    be started four times over.
    """
    claimed = (
        await session.execute(text("SELECT sched.mark_started(:o)"), {"o": str(org_id)})
    ).scalar_one_or_none()
    return bool(claimed)


async def _enqueue_for_org(org_id: uuid.UUID) -> tuple[int, int]:
    """Queue one `connector.sync` per syncable connection. Returns (seen, enqueued).

    Runs inside `org_session`, so every statement is bounded by row-level security to
    this organisation — the id came from the schedule index, and it is a hint about where
    to look, never an authorization (ADR 0012).
    """
    seen = 0
    enqueued = 0
    async with org_session(org_id) as session:
        connections = (
            (
                await session.execute(
                    text("SELECT id FROM connections WHERE status = ANY(:states) ORDER BY id"),
                    {"states": list(SYNCABLE_STATES)},
                )
            )
            .scalars()
            .all()
        )

        for connection_id in connections:
            seen += 1
            key = f"connector.sync:{org_id}:{connection_id}"
            # The key "Sync now" uses, so a nightly run and a person clicking cannot
            # produce two walks of one connection.
            inserted = (
                await session.execute(
                    text(
                        "INSERT INTO jobs (id, org_id, kind, state, idempotency_key, payload_json) "
                        "VALUES (:id, :org, 'connector.sync', 'pending', :key, "
                        "cast(:payload AS jsonb)) "
                        "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "org": str(org_id),
                        "key": key,
                        "payload": f'{{"connection_id": "{connection_id}"}}',
                    },
                )
            ).first()
            if inserted is not None:
                enqueued += 1
                continue
            # A finished sync is reopened; an in-flight one is left alone, because that
            # job already IS tonight's sync. `failed` and `dead_letter` are reopened here
            # on purpose: a nightly retry of a connection that failed last night is the
            # point, and the attempt ladder still bounds it.
            reopened = (
                await session.execute(
                    text(
                        "UPDATE jobs SET state = 'pending', attempts = 0, locked_until = NULL, "
                        "next_attempt_at = NULL, error = NULL, failure_kind = NULL, "
                        "updated_at = now() "
                        "WHERE idempotency_key = :key "
                        "AND state IN ('completed', 'failed', 'dead_letter') "
                        "RETURNING id"
                    ),
                    {"key": key},
                )
            ).first()
            if reopened is not None:
                enqueued += 1
    return seen, enqueued


async def _ring(org_id: uuid.UUID) -> bool:
    """Wake the worker for this organisation. Best-effort, like every doorbell.

    Without a transport (dev without Cloud Tasks) the rows simply wait for the arq
    worker's own drain, which is the honest degradation ADR 0017 already describes.
    """
    try:
        doorbell = CloudTasksDoorbell.from_env()
    except MisconfiguredDoorbell:
        logger.warning("%s", {"event": "doorbell_failed", "reason": "misconfigured"})
        return False
    if doorbell is None:
        return False
    return await doorbell.ring(org_id)


async def sync_organisation(due: DueOrganisation) -> SyncRun | None:
    """One organisation's nightly run. `None` when another tick already claimed it."""
    async with unscoped_session() as session:
        if not await _claim(session, due.org_id):
            return None

    outcome = "success"
    seen = 0
    enqueued = 0
    try:
        seen, enqueued = await _enqueue_for_org(due.org_id)
    except Exception:
        # The class only, and never the message: a database error's text carries the
        # failing statement's bound parameters (§4.9), which is why the engine sets
        # `hide_parameters` — this belt costs nothing.
        logger.exception("%s", {"event": "scheduled_sync_failed", "org_id": str(due.org_id)})
        outcome = "failed"

    rung = await _ring(due.org_id) if enqueued else False

    async with unscoped_session() as session:
        await session.execute(
            text("SELECT sched.mark_finished(:o, :outcome, :connections, :enqueued)"),
            {
                "o": str(due.org_id),
                "outcome": outcome,
                "connections": seen,
                "enqueued": enqueued,
            },
        )

    run = SyncRun(
        org_id=due.org_id, connections=seen, enqueued=enqueued, outcome=outcome, rung=rung
    )
    logger.info(
        "%s",
        {
            "event": "scheduled_sync",
            "org_id": str(due.org_id),
            "timezone": due.timezone,
            "hour_local": due.hour_local,
            "connections": seen,
            "enqueued": enqueued,
            "outcome": outcome,
            "rung": rung,
        },
    )
    return run


async def run_due_syncs(*, now: datetime | None = None) -> list[SyncRun]:
    """One tick. Organisations are handled one at a time, in id order.

    Sequential rather than gathered: each one only writes a handful of rows and then rings
    a queue that is itself rate-limited, so concurrency here would buy milliseconds and
    cost the ability to read the log top to bottom.
    """
    moment = now or datetime.now(UTC)
    async with unscoped_session() as session:
        due = await due_organisations(session, now=moment)

    logger.info("%s", {"event": "scheduled_sync_tick", "due": len(due)})
    runs = [run for item in due if (run := await sync_organisation(item)) is not None]
    if runs:
        logger.info(
            "%s",
            {
                "event": "scheduled_sync_complete",
                "organisations": len(runs),
                "enqueued": sum(r.enqueued for r in runs),
            },
        )
    return runs


def main() -> int:
    """Exit code is the job's result: Cloud Run retries a non-zero, and should."""
    configure_logging()
    try:
        runs = asyncio.run(run_due_syncs())
    except Exception:
        logger.exception("%s", {"event": "scheduled_sync_tick_failed"})
        return 1
    # A tick with nothing due is the normal case — 95 of every 96 runs — and says so
    # rather than looking like a job that did nothing because it broke.
    logger.info("%s", {"event": "scheduled_sync_tick_done", "organisations": len(runs)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
