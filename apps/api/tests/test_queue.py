"""The doorbell is best-effort by contract: Redis being down costs latency, never a 500."""

from __future__ import annotations

import uuid

import pytest
from jutsu_api import queue
from jutsu_core.doorbell import (
    ENV_DRAIN_URL,
    ENV_QUEUE,
    ENV_SERVICE_ACCOUNT,
    CloudTasksDoorbell,
    MisconfiguredDoorbell,
)

CLOUD = {
    ENV_QUEUE: "projects/p/locations/asia-south1/queues/jutsu-drain",
    ENV_SERVICE_ACCOUNT: "jutsu-runtime@p.iam.gserviceaccount.com",
    ENV_DRAIN_URL: "https://jutsu-worker-x-el.a.run.app/drain",
}


def _unset_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CLOUD:
        monkeypatch.delenv(name, raising=False)


class TestDoorbell:
    async def test_an_unreachable_redis_returns_false_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _unset_cloud(monkeypatch)
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1")  # nothing listens here
        await queue.reset_pool()
        try:
            assert await queue.ring_doorbell(uuid.uuid4()) is False
        finally:
            await queue.reset_pool()


class TestTransportSelection:
    """One function, two transports (spec §5, ADR 0017): Cloud Tasks whenever its three
    variables are set, arq otherwise, and a half-set configuration is refused where a
    deploy would notice — at startup — never in a request."""

    def test_nothing_configured_means_arq(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _unset_cloud(monkeypatch)
        assert queue.transport() == "arq"

    async def test_cloud_tasks_is_preferred_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name, value in CLOUD.items():
            monkeypatch.setenv(name, value)
        rung: list[tuple[uuid.UUID, dict[str, object]]] = []

        async def fake_ring(self: CloudTasksDoorbell, org_id: uuid.UUID, **options: object) -> bool:
            rung.append((org_id, options))
            return True

        monkeypatch.setattr(CloudTasksDoorbell, "ring", fake_ring)
        org = uuid.uuid4()

        assert queue.transport() == "cloud_tasks"
        assert await queue.ring_doorbell(org) is True
        # Deferred past the commit, exactly as the Redis doorbell defers.
        assert rung == [(org, {"delay_seconds": 2})]

    def test_a_partial_configuration_is_refused_at_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _unset_cloud(monkeypatch)
        monkeypatch.setenv(ENV_QUEUE, CLOUD[ENV_QUEUE])
        with pytest.raises(MisconfiguredDoorbell):
            queue.transport()

    async def test_a_partial_configuration_never_fails_a_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _unset_cloud(monkeypatch)
        monkeypatch.setenv(ENV_QUEUE, CLOUD[ENV_QUEUE])
        assert await queue.ring_doorbell(uuid.uuid4()) is False
