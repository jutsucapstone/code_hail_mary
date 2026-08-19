"""Smoke test for the worker entry point."""

from jutsu_worker.main import WorkerSettings, ping


async def test_ping_returns_pong() -> None:
    assert await ping({}) == "pong"


def test_ping_is_registered() -> None:
    """A job that is not in `functions` is unreachable from the queue."""
    assert ping in WorkerSettings.functions
