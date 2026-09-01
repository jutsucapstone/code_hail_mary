"""The doorbell half of ADR 0012.

The API writes durable job rows into Postgres — that is the queue, and it never lies.
What Postgres cannot do is wake the worker, and until this module existed nothing did:
a `connector.sync` row enqueued by "Sync now" sat `pending` for ever unless a CLI drain
happened to run. This publishes the wake-up.

Three properties are load-bearing:

  * **Best-effort, never a failure.** The row is already committed (or about to be); a
    doorbell lost to a Redis restart costs latency, not work — the next doorbell for the
    same org drains the whole backlog, because the worker's handler is a full per-org
    drain rather than a single-job poke.
  * **Org-scoped by construction.** The message carries the org id the session was
    already scoped to. There is no cross-tenant enumeration anywhere: the worker cannot
    list orgs (RLS holds the app role to one at a time), and this module never needs to.
  * **Deferred past the commit.** The request's transaction commits in FastAPI's
    teardown, *after* the handler returns — so the message is deferred a moment rather
    than racing the commit and finding an empty queue.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from uuid import UUID

from arq.connections import ArqRedis, RedisSettings, create_pool

logger = logging.getLogger("jutsu.api.queue")

DEFAULT_REDIS_URL = "redis://localhost:6379"

#: How long past the handler the worker should wait before draining. The request
#: transaction commits in dependency teardown; two seconds is comfortably past it and
#: invisible next to a provider sync.
_DEFER = timedelta(seconds=2)

#: Cap on how long a request will wait for a Redis connection before giving up on the
#: doorbell. The job row is durable either way.
_CONNECT_TIMEOUT_S = 2.0

_pool: ArqRedis | None = None
_lock = asyncio.Lock()


async def _get_pool() -> ArqRedis:
    global _pool
    async with _lock:
        if _pool is None:
            settings = RedisSettings.from_dsn(os.environ.get("REDIS_URL", DEFAULT_REDIS_URL))
            _pool = await create_pool(settings, retry=1)
        return _pool


async def reset_pool() -> None:
    """Drop the cached pool. Tests that repoint REDIS_URL call this, mirroring
    `dispose_engine`."""
    global _pool
    async with _lock:
        if _pool is not None:
            await _pool.aclose()
            _pool = None


async def ring_doorbell(org_id: UUID) -> bool:
    """Ask the worker to drain this organisation's queue. Returns whether the message
    was published — callers treat `False` as "the row will wait", never as an error.
    """
    try:
        pool = await asyncio.wait_for(_get_pool(), timeout=_CONNECT_TIMEOUT_S)
        await pool.enqueue_job("drain_org_jobs", str(org_id), _defer_by=_DEFER)
    except TimeoutError:
        logger.info("%s", {"event": "doorbell_skipped", "reason": "redis_timeout"})
        return False
    except OSError:
        logger.info("%s", {"event": "doorbell_skipped", "reason": "redis_unreachable"})
        return False
    except Exception:
        # A failed doorbell must never fail the request that rang it: the job row is
        # durable and a later doorbell drains it. Logged without the exception text —
        # a Redis error message is infrastructure detail, not something to forward.
        logger.warning("%s", {"event": "doorbell_failed"})
        return False
    return True
