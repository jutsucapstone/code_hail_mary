"""The production doorbell: Cloud Tasks → the worker's `/drain` (spec §5, §20).

Spec §5 splits the queue transport — arq + Redis in dev, Cloud Tasks in prod — "behind
one interface". Postgres is the queue in both (ADR 0012: a job exists because a row
exists); a transport only wakes a worker. This module is the production half. The API
rings it when it enqueues a job and the worker rings it again when a drain stops with
work left, so it lives in `jutsu_core`, which §6 names as the home of "jobs", and it
imports the Google client lazily so the package stays import-cheap for everything that
never rings.

The contract is the Redis doorbell's: best-effort, org-scoped by construction, deferred
past the caller's commit. A task that cannot be created costs latency, never work.

What this transport adds is coalescing. A task is named `{bucket}-{org}-{window}`, so a
burst of rings inside one window collapses into a single dispatch and ALREADY_EXISTS is
success. The window is short because Cloud Tasks tombstones a used name for about an
hour after the task runs: a name without the window would ring once and then be refused
for an hour, which is a doorbell that works exactly once (ADR 0017).

**A task is scheduled from the end of its window, never from the ring.** That is the
invariant coalescing rests on, and it is not obvious: with `schedule_time = now + delay`
and a window longer than the delay, the task named for window *w* fires while *w* is
still open, and every later ring in that window is answered ALREADY_EXISTS against a
name Cloud Tasks has already tombstoned — reported as success, with no dispatch coming
for the row that rang. Scheduling at `(w + 1) * window + delay` puts the dispatch after
every ring the window can contain, so "already scheduled" is always true when it is
claimed. The cost is bounded and small: at most `window + delay` seconds of latency.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

__all__ = [
    "DEFAULT_DELAY_SECONDS",
    "DEFAULT_WINDOW_SECONDS",
    "DISPATCH_DEADLINE_SECONDS",
    "ENV_DRAIN_URL",
    "ENV_QUEUE",
    "ENV_SERVICE_ACCOUNT",
    "CloudTasksDoorbell",
    "MisconfiguredDoorbell",
    "TaskCreator",
    "audience_for",
    "reset_client",
    "task_id",
]

logger = logging.getLogger("jutsu.doorbell")

#: The queue's full resource name: projects/P/locations/L/queues/Q.
ENV_QUEUE = "CLOUD_TASKS_QUEUE"
#: The service account the task's OIDC token names — the one identity the private
#: worker service lets in.
ENV_SERVICE_ACCOUNT = "CLOUD_TASKS_SERVICE_ACCOUNT"
#: Where the task is delivered: the worker service's `/drain`.
ENV_DRAIN_URL = "WORKER_DRAIN_URL"

#: Past the caller's commit. The request transaction commits in dependency teardown,
#: after the handler returns; two seconds is comfortably past it.
DEFAULT_DELAY_SECONDS = 2
#: Rings inside one window share a task name and therefore one dispatch.
DEFAULT_WINDOW_SECONDS = 5
#: A request must not wait on the queue. The row is durable either way.
_CREATE_TIMEOUT_S = 5.0

#: How long Cloud Tasks waits for `/drain` before calling the attempt failed. Set on the
#: task rather than left to the queue's default (600 s for an HTTP target), because a
#: drain is bounded at 480 s of *starting* work and the last job it started runs on: one
#: embedding job obeying five 120 s `Retry-After` hints is ~600 s by itself. 1800 s is
#: the maximum an HTTP target accepts, and matches the worker's Cloud Run request
#: timeout — under both, the queue would retry a drain that is merely slow, and two
#: drains for one organisation would then race for the same leases.
DISPATCH_DEADLINE_SECONDS = 1800


class MisconfiguredDoorbell(RuntimeError):
    """Part of the Cloud Tasks configuration is set and part is missing.

    Raised at startup by the callers that check, so a mis-deployed service fails to
    start rather than enqueueing rows nobody ever drains — a doorbell that silently
    never rings is a queue that silently never moves.
    """


class TaskCreator(Protocol):
    """The one method of the Cloud Tasks client this module uses. Injectable so the
    contract is tested against a client that misbehaves on purpose."""

    # `timeout` mirrors the GAPIC client's own keyword; this is a signature, not a
    # place where asyncio.timeout could stand in for it.
    async def create_task(self, *, parent: str, task: Any, timeout: float) -> Any: ...  # noqa: ASYNC109


def task_id(bucket: str, org_id: UUID, *, window_seconds: int, now: float) -> str:
    """Deterministic within a window, so a burst of rings is one dispatch."""
    return f"{bucket}-{org_id}-{int(now // window_seconds)}"


def audience_for(url: str) -> str:
    """Cloud Run validates the OIDC audience against the service origin, not the path."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


_client: Any = None


async def _shared_client() -> Any:
    global _client
    if _client is None:
        from google.cloud import tasks_v2

        # Application Default Credentials: on Cloud Run this is the runtime service
        # account, which holds `cloudtasks.enqueuer` on the queue and `actAs` on itself.
        _client = tasks_v2.CloudTasksAsyncClient()
    return _client


async def reset_client() -> None:
    """Drop the cached client. Tests that repoint the environment call this."""
    global _client
    _client = None


@dataclass(frozen=True)
class CloudTasksDoorbell:
    queue: str
    service_account: str
    drain_url: str

    @classmethod
    def from_env(cls, *, drain_url: str | None = None) -> CloudTasksDoorbell | None:
        """The configured doorbell, `None` when nothing is configured (dev: arq rings
        instead), and a refusal when only part of it is.

        `drain_url` may be supplied by a caller that knows its own address — the worker
        rings itself from the URL the request arrived on — and the environment is the
        fallback for the API, which learns the worker's URL at deploy time.
        """
        values = {
            name: os.environ.get(name, "").strip() for name in (ENV_QUEUE, ENV_SERVICE_ACCOUNT)
        }
        env_url = os.environ.get(ENV_DRAIN_URL, "").strip()
        # "Is this transport in use at all" is a question about the environment alone.
        # A caller-supplied `drain_url` is an address, not a signal: the worker always
        # knows its own, so counting it here would make dev's unconfigured door raise
        # `MisconfiguredDoorbell` instead of falling back — and the drain that had
        # already committed would answer 500 for want of a follow-up ring.
        if not any(values.values()) and not env_url:
            return None
        url = (drain_url or env_url).strip()
        missing = [name for name, value in values.items() if not value]
        if not url:
            missing.append(ENV_DRAIN_URL)
        if missing:
            raise MisconfiguredDoorbell(
                "the Cloud Tasks doorbell is partly configured; missing "
                + ", ".join(sorted(missing))
            )
        return cls(
            queue=values[ENV_QUEUE], service_account=values[ENV_SERVICE_ACCOUNT], drain_url=url
        )

    def build_task(
        self,
        org_id: UUID,
        *,
        bucket: str,
        delay_seconds: int,
        window_seconds: int,
        now: float,
    ) -> Any:
        """The task as Cloud Tasks will deliver it: a POST of `{"org_id"}` to the drain
        URL, signed with an OIDC token for the runtime service account, named so a burst
        inside one window is one dispatch, and scheduled from the END of that window so
        the dispatch cannot precede a ring it is supposed to cover."""
        from google.cloud import tasks_v2
        from google.protobuf import duration_pb2, timestamp_pb2

        request = tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=self.drain_url,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"org_id": str(org_id)}).encode(),
            oidc_token=tasks_v2.OidcToken(
                service_account_email=self.service_account,
                audience=audience_for(self.drain_url),
            ),
        )
        window_index = int(now // window_seconds)
        name = task_id(bucket, org_id, window_seconds=window_seconds, now=now)
        # The end of this window, then the delay. Never `now + delay` — see the module
        # docstring: that fires inside the window it is named for and tombstones the
        # name while rings are still coalescing onto it.
        scheduled = (window_index + 1) * window_seconds + delay_seconds
        return tasks_v2.Task(
            name=f"{self.queue}/tasks/{name}",
            http_request=request,
            schedule_time=timestamp_pb2.Timestamp(seconds=scheduled),
            dispatch_deadline=duration_pb2.Duration(seconds=DISPATCH_DEADLINE_SECONDS),
        )

    async def ring(
        self,
        org_id: UUID,
        *,
        bucket: str = "drain",
        delay_seconds: int = DEFAULT_DELAY_SECONDS,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        client: TaskCreator | None = None,
    ) -> bool:
        """Ask the worker to drain this organisation. Returns whether a dispatch is now
        scheduled — `False` means "the row will wait", never an error.
        """
        from google.api_core import exceptions as google_exceptions

        task = self.build_task(
            org_id,
            bucket=bucket,
            delay_seconds=delay_seconds,
            window_seconds=window_seconds,
            now=time.time(),
        )
        try:
            creator = client if client is not None else await _shared_client()
            await creator.create_task(parent=self.queue, task=task, timeout=_CREATE_TIMEOUT_S)
        except google_exceptions.AlreadyExists:
            # A dispatch for this window is already scheduled, and it fires after the
            # window closes, so it will drain what this ring is about. Logged rather
            # than silent: coalescing and a lost doorbell look identical from the
            # caller, and only this line tells them apart afterwards.
            logger.info(
                "%s",
                {
                    "event": "doorbell_coalesced",
                    "transport": "cloud_tasks",
                    "org_id": str(org_id),
                    "bucket": bucket,
                    "task": task.name.rsplit("/", 1)[-1],
                },
            )
            return True
        except google_exceptions.GoogleAPICallError as error:
            # The class, never the message: a Cloud Tasks error names resource paths
            # and is infrastructure detail, not something to forward.
            logger.warning(
                "%s",
                {
                    "event": "doorbell_failed",
                    "transport": "cloud_tasks",
                    "reason": type(error).__name__,
                },
            )
            return False
        except Exception:
            # A failed doorbell must never fail the request that rang it.
            logger.warning("%s", {"event": "doorbell_failed", "transport": "cloud_tasks"})
            return False
        logger.info(
            "%s",
            {
                "event": "doorbell_rung",
                "transport": "cloud_tasks",
                "org_id": str(org_id),
                "bucket": bucket,
                "delay_seconds": delay_seconds,
            },
        )
        return True
