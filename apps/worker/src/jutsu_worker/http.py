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
reclaimed by the next drain. What is caught is the *reporting*: one structured
`drain_failed` line naming the exception class, because otherwise the only record is
uvicorn's traceback and an operator cannot filter for it.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from jutsu_core.doorbell import ENV_DRAIN_URL, CloudTasksDoorbell, MisconfiguredDoorbell
from jutsu_core.logs import configure as configure_logging
from pydantic import BaseModel, ConfigDict

from jutsu_worker.drain import DrainReport, drain_and_report

__all__ = ["app", "create_app"]

logger = logging.getLogger("jutsu.worker")

#: Set by Cloud Tasks on every dispatch. Absent on anything that is not the queue.
TASK_HEADER = "x-cloudtasks-taskname"
QUEUE_HEADER = "x-cloudtasks-queuename"
#: How many times the queue has already tried this task, and how many times it has
#: reached the handler. A drain that keeps reappearing with a rising retry count is the
#: signature of work that fails the same way every time, and it is invisible without them.
RETRY_HEADER = "x-cloudtasks-taskretrycount"
EXECUTION_HEADER = "x-cloudtasks-taskexecutioncount"


class DrainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: UUID


def _configure_logging() -> None:
    """One JSON line per record, uvicorn's own loggers included.

    `basicConfig` was wrong twice here: it passed `LOG_LEVEL` through unvalidated, so a
    lowercase override raised `ValueError` at lifespan start and the revision never
    became Ready; and it left uvicorn's non-propagating loggers alone, so the traceback
    from a failed drain went out as plain text beside the JSON stream.
    """
    configure_logging()


def _doorbell_for(request: Request) -> CloudTasksDoorbell | None:
    """The doorbell the worker rings itself with, addressed to the URL this request
    arrived on — Cloud Run terminates TLS, so the scheme is https whatever the
    proxied request says. `WORKER_DRAIN_URL`, when set, wins."""
    configured = os.environ.get(ENV_DRAIN_URL, "").strip()
    self_url = configured or f"https://{request.url.netloc}/drain"
    return CloudTasksDoorbell.from_env(drain_url=self_url)


def _ring_target(request: Request) -> CloudTasksDoorbell | None:
    """`_doorbell_for`, but never raising into a request that has already drained.

    Startup refuses a half-configured deploy, so reaching this with one is the narrow
    case of an environment mutated under a running process. The drain is committed by
    the time a follow-up is considered; turning that into a 500 would make the queue
    re-run the whole drain, up to ten times, over a configuration error.
    """
    try:
        return _doorbell_for(request)
    except MisconfiguredDoorbell:
        logger.warning("%s", {"event": "doorbell_failed", "reason": "misconfigured"})
        return None


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

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """One filterable line for a failed drain, then the 500 the queue needs.

        Starlette re-raises after this returns, so uvicorn still records the traceback —
        now through the JSON formatter, and with `hide_parameters=True` on the engine it
        can no longer carry document text or an address (§4.9). What this adds is the
        *event*: `drain_failed` with the organisation and the task, so a failing drain is
        a log query rather than a search through tracebacks.
        """
        logger.error(
            "%s",
            {
                "event": "drain_failed",
                "task": request.headers.get(TASK_HEADER),
                "retry_count": request.headers.get(RETRY_HEADER),
                # The class, never the message: an exception's text is where content
                # leaks, and the traceback beside this line already has the detail.
                "reason": type(exc).__name__,
            },
        )
        return JSONResponse(status_code=500, content={"error": "drain_failed"})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness. Answers whether the process is up, nothing more.

        Note that Cloud Run's frontend answers `/healthz` itself and this never runs
        there; the deploy checks the revision's own Ready condition instead.
        """
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

        started = time.monotonic()
        report: DrainReport = await drain_and_report(payload.org_id)

        rung = False
        delay = report.follow_up_seconds
        if delay is not None:
            doorbell = _ring_target(request)
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
                "retry_count": request.headers.get(RETRY_HEADER),
                "execution_count": request.headers.get(EXECUTION_HEADER),
                "jobs": sum(report.counts.values()),
                "counts": report.counts,
                "claimable_now": report.claimable_now,
                "retries_waiting": report.retries_waiting,
                "leases_held": report.leases_held,
                "follow_up": report.follow_up,
                "follow_up_seconds": delay,
                "rung": rung,
                "elapsed_s": round(time.monotonic() - started, 3),
            },
        )
        return {
            "org_id": str(payload.org_id),
            "jobs": report.counts,
            "claimable_now": report.claimable_now,
            "retries_waiting": report.retries_waiting,
            "leases_held": report.leases_held,
            "follow_up": report.follow_up,
            "rung": rung,
        }

    return app


app = create_app()
