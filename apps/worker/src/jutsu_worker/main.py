"""Worker entry point.

The ingestion pipeline (§9) runs here as separately queued stages, so a failure at
`extract` never forces a re-`fetch`. S0 only proves the queue interface resolves; the
stages land in S8.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar


async def ping(ctx: dict[str, Any]) -> str:
    """No-op job. Exists so the queue wiring is exercised before S8 depends on it."""
    return "pong"


class WorkerSettings:
    """arq settings. Queue access is behind this one interface so the prod swap to
    Cloud Tasks (§5) touches nothing else."""

    functions: ClassVar[list[object]] = [ping]
    redis_settings = os.environ.get("REDIS_URL", "redis://localhost:6379")
    max_jobs = 10
    job_timeout = 600
