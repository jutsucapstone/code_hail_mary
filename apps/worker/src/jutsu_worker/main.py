"""Worker entry point.

The ingestion pipeline (§9) runs here as separately queued stages, so a failure at
`extract` never forces a re-`fetch`. S0 only proves the queue interface resolves; the
stages land in S8.

**Maintenance runs here too, and the first job is not optional.** Migration 0007 gives
`auth.pending_registrations` an `expires_at`, and an expiry column with nothing deleting
it is a comment rather than a control: the rows stop being *usable* on their own — the
consuming function filters on `expires_at > now()` — but they do not stop *existing*.
Each one holds a name, a work address and a job title in a schema that sits outside every
tenant boundary, so leaving them is a data-retention problem, not a correctness one.
"""

from __future__ import annotations

import logging
import os
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings
from jutsu_db import unscoped_session
from sqlalchemy import text

DEFAULT_REDIS_URL = "redis://localhost:6379"

logger = logging.getLogger("jutsu.worker")

#: Every five minutes. The challenge TTL is ten, so an abandoned registration's details
#: exist for at most about a quarter of an hour rather than indefinitely. The work is one
#: indexed DELETE against a table that is empty most of the time, so a tighter schedule
#: costs effectively nothing and shortens how long personal data sits in `auth`.
#: A plain `set`, not a `frozenset`: arq's `cron` is typed `set[int] | int | None` and
#: an immutable one is not a subtype of it.
REAP_MINUTES = set(range(0, 60, 5))


async def ping(ctx: dict[str, Any]) -> str:
    """No-op job. Exists so the queue wiring is exercised before S8 depends on it."""
    return "pong"


async def reap_expired_registrations(ctx: dict[str, Any]) -> int:
    """Delete staged registrations, challenges and rate-limit rows that have aged out.

    `unscoped_session` on purpose, and it is the sanctioned use rather than a shortcut:
    these tables carry no `org_id` at all, so there is no tenant to scope to. Its
    docstring names maintenance of exactly this kind as legitimate.

    Returns the number of pending registrations removed so a run is observable. The count
    is a number of rows and never their contents — §4.9 keeps names and addresses out of
    logs, and this is the one job that handles both.
    """
    async with unscoped_session() as session:
        removed = (
            await session.execute(text("SELECT auth.reap_expired_registrations()"))
        ).scalar_one()

    count = int(removed)
    if count:
        logger.info(
            "%s", {"event": "registrations_reaped", "removed": count}, extra={"removed": count}
        )
    return count


class WorkerSettings:
    """arq settings. Queue access is behind this one interface so the prod swap to
    Cloud Tasks (§5) touches nothing else."""

    functions: ClassVar[list[object]] = [ping]

    #: `run_at_startup` so a deploy clears whatever accumulated while nothing was
    #: running, rather than waiting for the next slot. `unique` is arq's default and is
    #: load-bearing once there is more than one worker: without it every replica runs
    #: the same DELETE at the same instant and they contend for the same rows.
    cron_jobs: ClassVar[list[Any]] = [
        cron(
            reap_expired_registrations,
            minute=REAP_MINUTES,
            run_at_startup=True,
            unique=True,
        )
    ]

    #: Parsed, not passed through as a string. arq's `create_pool` reads `.host`, `.port`
    #: and `.database` off this object, so assigning the raw DSN raises AttributeError the
    #: moment a worker actually starts — a failure no test caught, because the suite called
    #: the job function directly and never constructed a Worker.
    redis_settings = RedisSettings.from_dsn(os.environ.get("REDIS_URL", DEFAULT_REDIS_URL))
    max_jobs = 10
    job_timeout = 600
