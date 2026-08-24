"""Run the reaper once and exit. The entrypoint for a scheduled Cloud Run job.

The same job also exists as an arq cron in `main.py`, and both call the same function.
Two invocation paths rather than two implementations, because they answer different
questions: arq's scheduler is right when a worker process is already running for the
ingestion queue, and a one-shot job is right when nothing is.

**Deployed as a job, not as the worker service.** The arq scheduler lives inside the
process, so running it on Cloud Run means a container that never idles — `min-instances=1`
with CPU throttling off, billed continuously — plus Redis for arq to talk to. Memorystore
alone would cost more than every other piece of this deployment put together, to delete a
handful of rows every five minutes. Cloud Scheduler invoking this costs nothing at idle
and needs no broker.

The arq cron stays because it is correct for the moment the ingestion pipeline (S8) puts a
worker on Cloud Run for its own reasons. At that point the job can go, and the reaper is
already wired into the thing that is running.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from jutsu_worker.main import reap_expired_registrations

#: Re-exported deliberately. The job and the arq cron must invoke one function, never two
#: implementations that drift — and a test asserts they are the same object.
__all__ = ["main", "reap_expired_registrations"]

logger = logging.getLogger("jutsu.worker")


def main() -> int:
    """Exit code is the job's result: Cloud Run retries a non-zero, and should."""
    logging.basicConfig(
        level=logging.INFO,
        format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
        stream=sys.stdout,
    )
    try:
        removed = asyncio.run(reap_expired_registrations({}))
    except Exception:
        # Logged with the traceback and re-signalled through the exit code rather than
        # re-raised: an unhandled exception here prints a traceback Cloud Run records as
        # a crash, which is harder to distinguish from the container failing to start.
        logger.exception("reap_failed")
        return 1

    logger.info("%s", {"event": "reap_complete", "removed": removed})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
