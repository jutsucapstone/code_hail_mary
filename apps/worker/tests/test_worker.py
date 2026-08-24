"""Smoke tests for the worker entry point.

`test_settings_are_usable_by_arq` exists because of a real defect: `redis_settings` was
assigned the raw `REDIS_URL` string, and arq reads `.host` off that attribute when it
builds a pool. The worker therefore raised `AttributeError` on every start. Nothing
caught it, because the only tests called `ping()` directly and never went near arq's
own machinery — so the suite proved the job worked and never proved the worker ran.

The reaper tests follow the same rule. Asserting that the function deletes rows proves
nothing about whether anything ever *calls* it — migration 0007 shipped with the SQL
function in place and no caller at all, which is indistinguishable from having no reaper.
So the schedule is asserted separately from the behaviour.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from itertools import pairwise
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from arq.connections import RedisSettings
from jutsu_worker.main import (
    DEFAULT_REDIS_URL,
    REAP_MINUTES,
    WorkerSettings,
    ping,
    reap_expired_registrations,
)

TEST_DB_ENV = "JUTSU_TEST_DATABASE_URL"
MIGRATION_DB_ENV = "JUTSU_TEST_MIGRATION_URL"

#: Resolved at import, not inside the fixture: `Path.resolve` touches the filesystem, and
#: blocking I/O inside a coroutine stalls the loop it is running on.
DB_PACKAGE = Path(__file__).resolve().parents[3] / "packages" / "db"


def _alembic_config(url: str) -> Config:
    cfg = Config(str(DB_PACKAGE / "alembic.ini"))
    cfg.set_main_option("script_location", str(DB_PACKAGE / "src" / "jutsu_db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
async def worker_db_url(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """The restricted application role, against a migrated schema.

    Defined here rather than in a `conftest.py`, and that is not a style choice. Only one
    conftest may exist under `apps/`: mypy derives module names from the nearest directory
    without an `__init__.py`, so a second one is a duplicate module and mypy then refuses
    to check *anything*. The Makefile carries the same note.

    `jutsu_app` deliberately, not the owner. The reaper runs as the application, and the
    thing worth proving is that it may execute a definer function over tables it holds no
    grant on. Running this as the owner would pass whether or not that grant exists.

    Migrates forward only, never tears down. The API suite opens each of its tests with
    `downgrade base` then `upgrade head`, so it repairs whatever state it finds; tearing
    down here would only open a window where the two suites disagree about whether the
    schema exists, decided by collection order.
    """
    app_url = os.environ.get(TEST_DB_ENV)
    if not app_url:
        pytest.skip(f"{TEST_DB_ENV} is unset")

    migration_url = os.environ.get(MIGRATION_DB_ENV, app_url)

    # Alembic's env.py ends in `asyncio.run`, which cannot nest inside a running loop.
    monkeypatch.setenv("DATABASE_URL", migration_url)
    await asyncio.to_thread(command.upgrade, _alembic_config(migration_url), "head")

    yield app_url


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


class TestReaperSchedule:
    """That the reaper is *scheduled*, which is the half that is easy to omit."""

    def test_it_is_registered_as_a_cron_job(self) -> None:
        scheduled = [job.coroutine for job in WorkerSettings.cron_jobs]
        assert reap_expired_registrations in scheduled, (
            "the function existed with no caller once already; a reaper nothing runs "
            "is an expiry column with a comment attached"
        )

    def test_it_runs_often_enough_to_bound_retention(self) -> None:
        """Tighter than the ten-minute challenge TTL it cleans up after.

        The rows are already unusable once expired — the consuming function filters on
        `expires_at` — so this interval is about how long a name, an address and a job
        title sit in the org-less `auth` schema, not about correctness.
        """
        assert REAP_MINUTES, "an empty schedule means it never runs"
        assert max(b - a for a, b in pairwise(sorted(REAP_MINUTES))) <= 10

    def test_it_runs_at_startup(self) -> None:
        """So a deploy clears whatever accumulated while nothing was running."""
        job = next(j for j in WorkerSettings.cron_jobs if j.coroutine is reap_expired_registrations)
        assert job.run_at_startup

    def test_it_is_unique_across_workers(self) -> None:
        """Several replicas firing the same DELETE at the same instant just contend."""
        job = next(j for j in WorkerSettings.cron_jobs if j.coroutine is reap_expired_registrations)
        assert job.unique


@pytest.mark.skipif(
    os.environ.get("JUTSU_DB_REACHABLE") != "1",
    reason=f"nothing listening at {TEST_DB_ENV} — start Postgres with `make up`",
)
class TestReaperBehaviour:
    """That it deletes, against a real database.

    The DELETE is inside a `SECURITY DEFINER` function the application role may execute
    but whose table it cannot touch, so this also exercises that the grant is right —
    the failure mode being a permission error at 03:00 rather than in review.
    """

    async def test_it_removes_expired_rows_and_leaves_live_ones(self, worker_db_url: str) -> None:
        """Fresh token hashes each run, because the fixture migrates forward and never
        tears down — fixed keys collided on `pk_pending_registrations` the second time
        this ran, which looks like a reaper bug and is a test that cannot repeat."""
        import secrets

        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        expired_token = secrets.token_bytes(32)
        live_token = secrets.token_bytes(32)

        engine = create_async_engine(worker_db_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                # Two staged registrations: one aged out, one still live. A reaper that
                # deletes on `expires_at` alone would take both, and the symptom would be
                # a registrant told their perfectly valid link is invalid.
                await session.execute(
                    text(
                        "SELECT auth.stage_registration("
                        "  :expired, gen_random_uuid(), "
                        "  decode(repeat('b2', 32), 'hex'), 'expired.example', "
                        "  '{}'::jsonb, now() - interval '1 hour')"
                    ),
                    {"expired": expired_token},
                )
                await session.execute(
                    text(
                        "SELECT auth.stage_registration("
                        "  :live, gen_random_uuid(), "
                        "  decode(repeat('d4', 32), 'hex'), 'live.example', "
                        "  '{}'::jsonb, now() + interval '1 hour')"
                    ),
                    {"live": live_token},
                )
                await session.commit()

                removed = (
                    await session.execute(text("SELECT auth.reap_expired_registrations()"))
                ).scalar_one()
                await session.commit()

                assert removed >= 1

                # The live one survives, and is still consumable.
                survived = (
                    await session.execute(
                        text("SELECT domain FROM auth.consume_pending_registration(  :live)"),
                        {"live": live_token},
                    )
                ).scalar_one_or_none()
                assert survived == "live.example"

                # The expired one is gone, not merely unusable.
                gone = (
                    await session.execute(
                        text("SELECT domain FROM auth.consume_pending_registration(  :expired)"),
                        {"expired": expired_token},
                    )
                ).scalar_one_or_none()
                assert gone is None
                await session.commit()
        finally:
            await engine.dispose()


class TestReaperJobEntrypoint:
    """The one-shot path, which is how the reaper actually runs in production.

    Deployed as a scheduled Cloud Run job rather than as the arq worker: the scheduler
    lives inside the arq process, so running it on Cloud Run means a container that never
    idles plus Redis for arq to talk to — more cost than everything else here combined, to
    delete a few rows every five minutes.
    """

    def test_it_reports_success_through_the_exit_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jutsu_worker import reap

        async def _reaped(_ctx: object) -> int:
            return 3

        monkeypatch.setattr(reap, "reap_expired_registrations", _reaped)
        assert reap.main() == 0

    def test_a_failure_is_a_non_zero_exit_and_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cloud Run retries a non-zero exit. An unhandled exception instead prints a
        crash, which reads like the container failing to start rather than the job
        failing to do its work."""
        from jutsu_worker import reap

        async def _exploded(_ctx: object) -> int:
            raise RuntimeError("database unreachable")

        monkeypatch.setattr(reap, "reap_expired_registrations", _exploded)
        assert reap.main() == 1

    def test_both_paths_call_the_same_function(self) -> None:
        """Two ways to invoke it, never two implementations — otherwise the scheduled
        job and the cron drift and only one of them is ever exercised."""
        from jutsu_worker import main as worker_main
        from jutsu_worker import reap

        assert reap.reap_expired_registrations is worker_main.reap_expired_registrations
