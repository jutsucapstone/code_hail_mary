"""The Cloud Tasks doorbell: what a ring sends, how a burst coalesces, and why a failure
never reaches the caller.

Everything here runs against a fake client. The real one needs Google credentials and a
queue, and a unit test that reached Cloud Tasks would enqueue work in somebody's project
per assertion. The task object itself is the real proto type, so the fields asserted are
the fields Cloud Tasks will read.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest
from google.api_core.exceptions import AlreadyExists, PermissionDenied
from jutsu_core.doorbell import (
    DEFAULT_DELAY_SECONDS,
    ENV_DRAIN_URL,
    ENV_QUEUE,
    ENV_SERVICE_ACCOUNT,
    CloudTasksDoorbell,
    MisconfiguredDoorbell,
    audience_for,
    task_id,
)

QUEUE = "projects/p/locations/asia-south1/queues/jutsu-drain"
SA = "jutsu-runtime@p.iam.gserviceaccount.com"
DRAIN = "https://jutsu-worker-abc-el.a.run.app/drain"


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, Any, float]] = []
        self.error = error

    async def create_task(self, *, parent: str, task: Any, timeout: float) -> Any:  # noqa: ASYNC109
        self.calls.append((parent, task, timeout))
        if self.error is not None:
            raise self.error
        return task


def doorbell() -> CloudTasksDoorbell:
    return CloudTasksDoorbell(queue=QUEUE, service_account=SA, drain_url=DRAIN)


class TestNaming:
    def test_rings_inside_one_window_share_a_name_and_the_next_window_gets_a_new_one(self) -> None:
        org = uuid.uuid4()
        first = task_id("drain", org, window_seconds=5, now=1000.0)
        assert task_id("drain", org, window_seconds=5, now=1004.9) == first
        assert task_id("drain", org, window_seconds=5, now=1005.0) != first

    def test_the_name_carries_the_bucket_and_the_org(self) -> None:
        org = uuid.uuid4()
        assert task_id("drain-retry", org, window_seconds=60, now=0) == f"drain-retry-{org}-0"

    def test_the_audience_is_the_service_origin_not_the_path(self) -> None:
        assert audience_for(DRAIN) == "https://jutsu-worker-abc-el.a.run.app"


class TestConfiguration:
    def test_nothing_configured_means_no_cloud_tasks_doorbell(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in (ENV_QUEUE, ENV_SERVICE_ACCOUNT, ENV_DRAIN_URL):
            monkeypatch.delenv(name, raising=False)
        assert CloudTasksDoorbell.from_env() is None

    def test_a_supplied_drain_url_is_not_itself_a_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worker always knows its own address, so it always passes one. That must
        not be read as "Cloud Tasks is configured" — in dev nothing is, and the answer
        has to be `None` so the caller falls back rather than raising over a follow-up
        ring on a drain that already committed."""
        for name in (ENV_QUEUE, ENV_SERVICE_ACCOUNT, ENV_DRAIN_URL):
            monkeypatch.delenv(name, raising=False)
        assert CloudTasksDoorbell.from_env(drain_url=DRAIN) is None

    def test_a_partial_configuration_is_refused_and_names_what_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_QUEUE, QUEUE)
        monkeypatch.delenv(ENV_SERVICE_ACCOUNT, raising=False)
        monkeypatch.delenv(ENV_DRAIN_URL, raising=False)
        with pytest.raises(MisconfiguredDoorbell) as refused:
            CloudTasksDoorbell.from_env()
        assert ENV_SERVICE_ACCOUNT in str(refused.value)
        assert ENV_DRAIN_URL in str(refused.value)

    def test_a_caller_may_supply_its_own_drain_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The worker rings itself from the address the request arrived on."""
        monkeypatch.setenv(ENV_QUEUE, QUEUE)
        monkeypatch.setenv(ENV_SERVICE_ACCOUNT, SA)
        monkeypatch.delenv(ENV_DRAIN_URL, raising=False)
        configured = CloudTasksDoorbell.from_env(drain_url=DRAIN)
        assert configured == doorbell()


class TestTheTask:
    def test_it_posts_only_the_org_id_signed_for_the_runtime_account(self) -> None:
        org = uuid.uuid4()
        task = doorbell().build_task(
            org, bucket="drain", delay_seconds=2, window_seconds=5, now=1000
        )

        assert task.name == f"{QUEUE}/tasks/drain-{org}-200"
        assert task.http_request.url == DRAIN
        assert task.http_request.http_method.name == "POST"
        assert json.loads(task.http_request.body) == {"org_id": str(org)}
        assert task.http_request.oidc_token.service_account_email == SA
        assert task.http_request.oidc_token.audience == "https://jutsu-worker-abc-el.a.run.app"
        # Past the caller's commit — never at once.
        assert task.schedule_time.timestamp() == 1002


class TestRinging:
    async def test_a_ring_creates_one_task_on_the_queue(self) -> None:
        client = FakeClient()
        before = time.time()
        assert await doorbell().ring(uuid.uuid4(), client=client) is True
        ((parent, task, timeout),) = client.calls
        assert parent == QUEUE
        assert task.http_request.url == DRAIN
        assert timeout > 0
        # Deferred past the caller's commit, from the moment of the ring.
        assert task.schedule_time.timestamp() >= int(before) + DEFAULT_DELAY_SECONDS

    async def test_a_ring_already_scheduled_in_this_window_is_success(self) -> None:
        """ALREADY_EXISTS is the coalescing working, not a failure: the scheduled
        dispatch drains everything, including whatever rang now."""
        client = FakeClient(error=AlreadyExists("scheduled"))  # type: ignore[no-untyped-call]
        assert await doorbell().ring(uuid.uuid4(), client=client) is True

    async def test_a_refused_ring_returns_false_and_never_raises(self) -> None:
        client = FakeClient(error=PermissionDenied("no enqueuer role"))  # type: ignore[no-untyped-call]
        assert await doorbell().ring(uuid.uuid4(), client=client) is False

    async def test_an_unexpected_failure_returns_false_and_never_raises(self) -> None:
        client = FakeClient(error=RuntimeError("channel closed"))
        assert await doorbell().ring(uuid.uuid4(), client=client) is False

    async def test_the_bucket_and_delay_reach_the_task(self) -> None:
        client = FakeClient()
        org = uuid.uuid4()
        await doorbell().ring(
            org, bucket="drain-retry", delay_seconds=60, window_seconds=60, client=client
        )
        ((_, task, _),) = client.calls
        assert task.name.startswith(f"{QUEUE}/tasks/drain-retry-{org}-")
