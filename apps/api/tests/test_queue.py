"""The doorbell is best-effort by contract: Redis being down costs latency, never a 500."""

from __future__ import annotations

import uuid

import pytest
from jutsu_api import queue


class TestDoorbell:
    async def test_an_unreachable_redis_returns_false_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1")  # nothing listens here
        await queue.reset_pool()
        try:
            assert await queue.ring_doorbell(uuid.uuid4()) is False
        finally:
            await queue.reset_pool()
