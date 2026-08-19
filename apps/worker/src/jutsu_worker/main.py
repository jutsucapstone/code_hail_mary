"""Worker entry point.

The ingestion pipeline (§9) runs here as separately queued stages, so a failure at
`extract` never forces a re-`fetch`. S0 only proves the queue interface resolves; the
stages land in S8.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

from arq.connections import RedisSettings

DEFAULT_REDIS_URL = "redis://localhost:6379"


async def ping(ctx: dict[str, Any]) -> str:
    """No-op job. Exists so the queue wiring is exercised before S8 depends on it."""
    return "pong"


class WorkerSettings:
    """arq settings. Queue access is behind this one interface so the prod swap to
    Cloud Tasks (§5) touches nothing else."""

    functions: ClassVar[list[object]] = [ping]
    #: Parsed, not passed through as a string. arq's `create_pool` reads `.host`, `.port`
    #: and `.database` off this object, so assigning the raw DSN raises AttributeError the
    #: moment a worker actually starts — a failure no test caught, because the suite called
    #: the job function directly and never constructed a Worker.
    redis_settings = RedisSettings.from_dsn(os.environ.get("REDIS_URL", DEFAULT_REDIS_URL))
    max_jobs = 10
    job_timeout = 600
