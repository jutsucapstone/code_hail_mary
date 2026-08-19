"""Smoke tests for the worker entry point.

`test_settings_are_usable_by_arq` exists because of a real defect: `redis_settings` was
assigned the raw `REDIS_URL` string, and arq reads `.host` off that attribute when it
builds a pool. The worker therefore raised `AttributeError` on every start. Nothing
caught it, because the only tests called `ping()` directly and never went near arq's
own machinery — so the suite proved the job worked and never proved the worker ran.
"""

from arq.connections import RedisSettings
from jutsu_worker.main import DEFAULT_REDIS_URL, WorkerSettings, ping


async def test_ping_returns_pong() -> None:
    assert await ping({}) == "pong"


def test_ping_is_registered() -> None:
    """A job that is not in `functions` is unreachable from the queue."""
    assert ping in WorkerSettings.functions


def test_settings_are_usable_by_arq() -> None:
    """The attributes arq reads must actually exist.

    Asserting the type alone would pass against a subclass that omits them, so this
    reaches for the three attributes `create_pool` uses.
    """
    settings = WorkerSettings.redis_settings

    assert isinstance(settings, RedisSettings)
    assert settings.host == "localhost"
    assert settings.port == 6379
    assert settings.database == 0


def test_default_dsn_parses() -> None:
    """The fallback used when REDIS_URL is unset has to parse too.

    It is the path every fresh checkout takes before anyone writes a `.env`.
    """
    assert RedisSettings.from_dsn(DEFAULT_REDIS_URL).host == "localhost"
