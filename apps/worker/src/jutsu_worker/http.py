"""The worker as a Cloud Run service — one door, rung by Cloud Tasks (spec §5, §20).

Why a service rather than the arq process: production has no Redis, by the runbook's
own cost stance, and Cloud Tasks is the transport the spec names for it. An HTTP push
also lets the worker scale from zero — nothing runs or bills between doorbells, which
is the property the reaper job was built to preserve. The arq entry point in `main.py`
stays for dev, where Compose provides Redis; both dispatchers call the same drain.

Authorization is the platform's. The service is deployed `--no-allow-unauthenticated`
and only the runtime service account — the identity Cloud Tasks signs its OIDC token
with — holds `run.invoker`, so what arrives here has already been authenticated by
Cloud Run. The header check below is defence in depth against a mis-deploy that opened
the service, not the gate itself. The org id in the body is a hint about where to look,
never an authorization: row-level security decides what the drain can see (ADR 0012).

A drain that raises answers 500, and that is correct: Cloud Tasks retries the task on
the queue's backoff, and the lease on whatever job was mid-flight expires and is
reclaimed by the next drain. Nothing here catches what it cannot handle.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, status
from jutsu_core.doorbell import ENV_DRAIN_URL, CloudTasksDoorbell
from pydantic import BaseModel, ConfigDict

from jutsu_worker.drain import DrainReport, drain_and_report, follow_up_delay

__all__ = ["app", "create_app"]

logger = logging.getLogger("jutsu.worker")

#: Set by Cloud Tasks on every dispatch. Absent on anything that is not the queue.
TASK_HEADER = "x-cloudtasks-taskname"
QUEUE_HEADER = "x-cloudtasks-queuename"


class DrainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: UUID


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
        stream=sys.stdout,
        force=True,
    )


def _doorbell_for(request: Request) -> CloudTasksDoorbell | None:
    """The doorbell the worker rings itself with, addressed to the URL this request
    arrived on — Cloud Run terminates TLS, so the scheme is https whatever the
    proxied request says. `WORKER_DRAIN_URL`, when set, wins."""
    configured = os.environ.get(ENV_DRAIN_URL, "").strip()
    self_url = configured or f"https://{request.url.netloc}/drain"
    return CloudTasksDoorbell.from_env(drain_url=self_url)


#: Stands in for the worker's own address while validating configuration at startup.
#: The real one is the URL each request arrives on, which is not knowable yet — so this
#: checks the half that comes from the deploy, which is the half that can be wrong.
_UNKNOWN_SELF_URL = "https://worker.invalid/drain"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    # The same refusal the API makes (ADR 0017): a half-configured doorbell is a queue
    # that silently stops moving, and startup is where a deploy notices. `WORKER_DRAIN_URL`
    # is deliberately NOT set on this service, so it must not be required here.
    doorbell = CloudTasksDoorbell.from_env(
        drain_url=os.environ.get(ENV_DRAIN_URL, "").strip() or _UNKNOWN_SELF_URL
    )
    # Announce the transport this process actually has. "none" is the honest answer with
    # nothing configured: the drain still runs, but a leftover cannot re-ring itself —
    # there is no arq fallback on this door.
    logger.info(
        "%s",
        {
            "event": "worker_started",
            "transport": "cloud_tasks" if doorbell is not None else "none",
        },
    )
    yield
    logger.info("%s", {"event": "worker_stopped"})


def create_app() -> FastAPI:
    app = FastAPI(
        title="jutsu-worker",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness. Answers whether the process is up, nothing more."""
        return {"status": "ok"}

    @app.post("/drain")
    async def drain(payload: DrainRequest, request: Request) -> dict[str, Any]:
        """Drain one organisation's queue, then ring again if anything is left."""
        task = request.headers.get(TASK_HEADER)
        if task is None and os.environ.get("JUTSU_ENV") == "prod":
            # Cloud Run's IAM check is the gate; this refuses a request that reached a
            # service somebody opened by mistake, and says nothing about the queue.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the drain queue rings this door.",
            )

        report: DrainReport = await drain_and_report(payload.org_id)

        rung = False
        delay = follow_up_delay(report.follow_up)
        if delay is not None:
            doorbell = _doorbell_for(request)
            if doorbell is not None:
                rung = await doorbell.ring(
                    payload.org_id,
                    bucket=f"drain-{report.follow_up}",
                    delay_seconds=delay,
                    window_seconds=delay,
                )

        logger.info(
            "%s",
            {
                "event": "drain_complete",
                "org_id": str(payload.org_id),
                "task": task,
                "queue": request.headers.get(QUEUE_HEADER),
                "jobs": sum(report.counts.values()),
                "follow_up": report.follow_up,
                "rung": rung,
            },
        )
        return {
            "org_id": str(payload.org_id),
            "jobs": report.counts,
            "claimable_now": report.claimable_now,
            "retries_waiting": report.retries_waiting,
            "follow_up": report.follow_up,
            "rung": rung,
        }

    return app


app = create_app()
