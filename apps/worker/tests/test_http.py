"""The worker's HTTP door, against a drain that is stubbed at the one seam.

What must hold: the body is exactly `{org_id}`; a leftover rings the doorbell with the
right bucket and delay and at the address the request arrived on; a request that did not
come from the queue is refused in production; and both dispatchers — arq and this door —
call the same `drain_and_report`, so the follow-up decision cannot drift between dev and
prod.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_core.doorbell import MisconfiguredDoorbell
from jutsu_worker import http as worker_http
from jutsu_worker import main as worker_main
from jutsu_worker.drain import (
    FOLLOW_UP_NOW_SECONDS,
    FOLLOW_UP_RETRY_SECONDS,
    DrainReport,
    follow_up_delay,
)
from starlette.requests import Request

ORG = uuid.uuid4()
COUNTS = {
    "ingest.source": 0,
    "ingest.document": 2,
    "embed.document": 2,
    "connector.sync": 1,
    "extract.document": 0,
}


class RecordingDoorbell:
    def __init__(self, drain_url: str) -> None:
        self.drain_url = drain_url
        self.rings: list[dict[str, Any]] = []

    async def ring(self, org_id: uuid.UUID, **options: Any) -> bool:
        self.rings.append({"org_id": org_id, **options})
        return True


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=worker_http.app)
    async with AsyncClient(transport=transport, base_url="https://jutsu-worker.example") as c:
        yield c


def _report(follow_up: Any, *, claimable_now: int = 0, retries_waiting: int = 0) -> DrainReport:
    return DrainReport(
        counts=COUNTS,
        claimable_now=claimable_now,
        retries_waiting=retries_waiting,
        follow_up=follow_up,
    )


class TestTheDoor:
    async def test_healthz_answers_without_a_database(self, client: AsyncClient) -> None:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_a_drain_reports_the_counts_and_rings_nothing_when_idle(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[uuid.UUID] = []

        async def fake_drain(org_id: uuid.UUID) -> DrainReport:
            seen.append(org_id)
            return _report(None)

        doorbell = RecordingDoorbell("https://jutsu-worker.example/drain")
        monkeypatch.setattr(worker_http, "drain_and_report", fake_drain)
        monkeypatch.setattr(worker_http, "_doorbell_for", lambda request: doorbell)

        response = await client.post("/drain", json={"org_id": str(ORG)})

        assert response.status_code == 200, response.text
        body = response.json()
        assert seen == [ORG]
        assert body["jobs"] == COUNTS
        assert body["follow_up"] is None
        assert body["rung"] is False
        assert doorbell.rings == []

    async def test_work_left_claimable_rings_again_almost_at_once_at_its_own_address(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_drain(org_id: uuid.UUID) -> DrainReport:
            return _report("now", claimable_now=7)

        captured: dict[str, RecordingDoorbell] = {}

        def doorbell_for(request: Any) -> RecordingDoorbell:
            doorbell = RecordingDoorbell(f"https://{request.url.netloc}/drain")
            captured["doorbell"] = doorbell
            return doorbell

        monkeypatch.setattr(worker_http, "drain_and_report", fake_drain)
        monkeypatch.setattr(worker_http, "_doorbell_for", doorbell_for)

        response = await client.post(
            "/drain",
            json={"org_id": str(ORG)},
            headers={"x-cloudtasks-taskname": "drain-x-1", "x-cloudtasks-queuename": "jutsu-drain"},
        )

        assert response.status_code == 200
        assert response.json()["follow_up"] == "now"
        assert response.json()["rung"] is True
        (ring,) = captured["doorbell"].rings
        assert ring["org_id"] == ORG
        assert ring["bucket"] == "drain-now"
        assert ring["delay_seconds"] == FOLLOW_UP_NOW_SECONDS
        assert captured["doorbell"].drain_url == "https://jutsu-worker.example/drain"

    async def test_only_waiting_retries_ring_after_the_shortest_backoff(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_drain(org_id: uuid.UUID) -> DrainReport:
            return _report("retry", retries_waiting=1)

        doorbell = RecordingDoorbell("https://jutsu-worker.example/drain")
        monkeypatch.setattr(worker_http, "drain_and_report", fake_drain)
        monkeypatch.setattr(worker_http, "_doorbell_for", lambda request: doorbell)

        response = await client.post("/drain", json={"org_id": str(ORG)})

        assert response.status_code == 200
        (ring,) = doorbell.rings
        assert ring["bucket"] == "drain-retry"
        assert ring["delay_seconds"] == FOLLOW_UP_RETRY_SECONDS

    async def test_the_body_is_exactly_an_org_id(self, client: AsyncClient) -> None:
        assert (await client.post("/drain", json={})).status_code == 422
        assert (await client.post("/drain", json={"org_id": "not-a-uuid"})).status_code == 422
        extra = await client.post("/drain", json={"org_id": str(ORG), "user_id": str(uuid.uuid4())})
        assert extra.status_code == 422

    async def test_in_production_only_the_queue_may_ring(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cloud Run's IAM is the gate; this is the belt over it. A request without the
        queue's header is refused before any drain runs."""
        drained: list[uuid.UUID] = []

        async def fake_drain(org_id: uuid.UUID) -> DrainReport:
            drained.append(org_id)
            return _report(None)

        monkeypatch.setattr(worker_http, "drain_and_report", fake_drain)
        monkeypatch.setenv("JUTSU_ENV", "prod")

        refused = await client.post("/drain", json={"org_id": str(ORG)})
        assert refused.status_code == 403
        assert drained == []

        allowed = await client.post(
            "/drain", json={"org_id": str(ORG)}, headers={"x-cloudtasks-taskname": "t-1"}
        )
        assert allowed.status_code == 200
        assert drained == [ORG]


def _request_to(host: str) -> Request:
    """A real request, so `_doorbell_for` reads the address the way Cloud Run delivers
    it: the Host header, not a stand-in attribute."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/drain",
            "query_string": b"",
            "headers": [(b"host", host.encode())],
            "scheme": "https",
            "server": (host, 443),
        }
    )


class TestTheRealDoorbellLookup:
    """`_doorbell_for` itself — every test above stubs it, which is how it shipped
    raising on the one environment that has no Cloud Tasks at all."""

    async def test_an_unconfigured_environment_answers_200_and_rings_nothing(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The drain has already committed by the time a follow-up is considered. With
        no Cloud Tasks configured the ring is simply not available — `rung: false`, the
        contract both transports state — and never a 500 over work that succeeded."""
        for name in ("CLOUD_TASKS_QUEUE", "CLOUD_TASKS_SERVICE_ACCOUNT", "WORKER_DRAIN_URL"):
            monkeypatch.delenv(name, raising=False)

        async def fake_drain(org_id: uuid.UUID) -> DrainReport:
            return _report("now", claimable_now=4)

        monkeypatch.setattr(worker_http, "drain_and_report", fake_drain)

        response = await client.post("/drain", json={"org_id": str(ORG)})

        assert response.status_code == 200, response.text
        assert response.json()["follow_up"] == "now"
        assert response.json()["rung"] is False

    def test_a_configured_worker_addresses_the_ring_at_the_url_it_was_reached_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No WORKER_DRAIN_URL is set on the worker service by deploy.yml — it derives
        its own from the request, so a ring lands back on the same service."""
        monkeypatch.setenv("CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/jutsu-drain")
        monkeypatch.setenv("CLOUD_TASKS_SERVICE_ACCOUNT", "runtime@p.iam.gserviceaccount.com")
        monkeypatch.delenv("WORKER_DRAIN_URL", raising=False)

        resolved = worker_http._doorbell_for(_request_to("jutsu-worker-abc-el.a.run.app"))

        assert resolved is not None
        assert resolved.drain_url == "https://jutsu-worker-abc-el.a.run.app/drain"

    def test_a_partly_configured_worker_still_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Half a configuration is a mis-deploy, and stays loud."""
        monkeypatch.setenv("CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/jutsu-drain")
        monkeypatch.delenv("CLOUD_TASKS_SERVICE_ACCOUNT", raising=False)
        monkeypatch.delenv("WORKER_DRAIN_URL", raising=False)

        with pytest.raises(MisconfiguredDoorbell):
            worker_http._doorbell_for(_request_to("jutsu-worker-abc-el.a.run.app"))


class TestStartup:
    """What the worker says it is, and what it refuses to start with (ADR 0017)."""

    async def test_it_announces_none_when_no_transport_is_configured(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """It shipped announcing `cloud_tasks` unconditionally. A drain still runs with
        nothing configured — it just cannot re-ring itself — and saying `cloud_tasks`
        there is a log line that would send someone looking at the wrong queue."""
        for name in ("CLOUD_TASKS_QUEUE", "CLOUD_TASKS_SERVICE_ACCOUNT", "WORKER_DRAIN_URL"):
            monkeypatch.delenv(name, raising=False)

        # `capsys`, not `caplog`: `_configure_logging` rebuilds the root handlers with
        # `force=True`, which drops the capture handler caplog installs. Stdout is where
        # the line actually goes — and where Cloud Run reads it.
        async with worker_http._lifespan(worker_http.app):
            pass

        assert "'transport': 'none'" in capsys.readouterr().out

    async def test_a_configured_worker_needs_no_drain_url_of_its_own(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """deploy.yml sets the queue and the account on this service and deliberately
        not `WORKER_DRAIN_URL` — the worker rings itself at the address a task arrives
        on. Requiring it at startup would refuse the real production configuration."""
        monkeypatch.setenv("CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/jutsu-drain")
        monkeypatch.setenv("CLOUD_TASKS_SERVICE_ACCOUNT", "runtime@p.iam.gserviceaccount.com")
        monkeypatch.delenv("WORKER_DRAIN_URL", raising=False)

        async with worker_http._lifespan(worker_http.app):
            pass

        assert "'transport': 'cloud_tasks'" in capsys.readouterr().out

    async def test_a_half_configured_worker_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A doorbell that silently never rings is a queue that silently never moves."""
        monkeypatch.setenv("CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/jutsu-drain")
        monkeypatch.delenv("CLOUD_TASKS_SERVICE_ACCOUNT", raising=False)
        monkeypatch.delenv("WORKER_DRAIN_URL", raising=False)

        with pytest.raises(MisconfiguredDoorbell):
            async with worker_http._lifespan(worker_http.app):
                pass


class TestOneDecisionTwoTransports:
    def test_follow_up_delays_are_the_documented_ones(self) -> None:
        assert follow_up_delay("now") == FOLLOW_UP_NOW_SECONDS
        assert follow_up_delay("retry") == FOLLOW_UP_RETRY_SECONDS
        assert follow_up_delay(None) is None

    async def test_the_arq_handler_drains_through_the_same_report_and_rings_redis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_drain(org_id: uuid.UUID) -> DrainReport:
            return _report("retry", retries_waiting=3)

        class FakeRedis:
            def __init__(self) -> None:
                self.jobs: list[tuple[Any, ...]] = []

            async def enqueue_job(self, name: str, *args: Any, **options: Any) -> None:
                self.jobs.append((name, args, options))

        monkeypatch.setattr(worker_main, "drain_and_report", fake_drain)
        redis = FakeRedis()

        counts = await worker_main.drain_org_jobs({"redis": redis}, str(ORG))

        assert counts == COUNTS
        ((name, args, options),) = redis.jobs
        assert name == "drain_org_jobs"
        assert args == (str(ORG),)
        assert options["_job_id"] == f"drain-retry:{ORG}"
        assert options["_defer_by"].total_seconds() == FOLLOW_UP_RETRY_SECONDS

    async def test_the_arq_handler_rings_at_once_when_work_is_claimable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_drain(org_id: uuid.UUID) -> DrainReport:
            return _report("now", claimable_now=1)

        class FakeRedis:
            def __init__(self) -> None:
                self.jobs: list[tuple[Any, ...]] = []

            async def enqueue_job(self, name: str, *args: Any, **options: Any) -> None:
                self.jobs.append((name, args, options))

        monkeypatch.setattr(worker_main, "drain_and_report", fake_drain)
        redis = FakeRedis()

        await worker_main.drain_org_jobs({"redis": redis}, str(ORG))

        ((_, _, options),) = redis.jobs
        assert options["_job_id"] == f"drain-more:{ORG}"
        assert options["_defer_by"].total_seconds() == FOLLOW_UP_NOW_SECONDS

    async def test_the_arq_handler_without_redis_still_drains(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_drain(org_id: uuid.UUID) -> DrainReport:
            return _report("now", claimable_now=1)

        monkeypatch.setattr(worker_main, "drain_and_report", fake_drain)
        assert await worker_main.drain_org_jobs({}, str(ORG)) == COUNTS
