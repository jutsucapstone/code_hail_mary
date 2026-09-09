"""The sync schedule: an org-less index a clock may read, and nothing else may (ADR 0018).

Two halves, and both matter. The clock has to be able to enumerate organisations — no
role in this deployment can read `orgs`, which is what blocked a nightly sync for the
whole life of the project. And the request path must *not* be able to read another
organisation's schedule, which is the price of putting the table outside row-level
security: privilege is the only thing containing it, so privilege is what these assert.

Times are checked against a real zone (`Asia/Kolkata`, +05:30) rather than UTC, because
a schedule that is only ever tested at UTC passes with the timezone ignored entirely.
"""

from __future__ import annotations

import pathlib
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


async def _scope(conn: AsyncConnection, org_id: uuid.UUID) -> None:
    await conn.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
    )


async def _clear_scope(conn: AsyncConnection) -> None:
    await conn.execute(text("SELECT set_config('app.current_org_id', '', true)"))


async def _age_claim(
    migration_url: str, org_id: uuid.UUID, *, minutes: int, finished: bool
) -> None:
    """Move a claim back in time, as the owner.

    The application role holds no privilege on this table — that is the containment
    `TestPrivilegeIsTheBoundary` asserts — so a test that needs to rewrite a timestamp
    has to do it from a connection that may, rather than pretend it can.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migration_url)
    try:
        async with engine.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE sched.org_sync_schedules "
                    "SET last_started_at = now() - make_interval(mins => :m), "
                    "    last_finished_at = CASE WHEN :fin "
                    "        THEN now() - make_interval(mins => :m - 1) ELSE last_finished_at END "
                    "WHERE org_id = :o"
                ),
                {"m": minutes, "fin": finished, "o": str(org_id)},
            )
    finally:
        await engine.dispose()


async def _set_claim(
    migration_url: str,
    org_id: uuid.UUID,
    *,
    started_at: datetime,
    finished_at: datetime | None,
) -> None:
    """Place a claim at an exact instant, as the owner.

    Absolute rather than `now() - interval` because dueness is evaluated against a
    `p_now` the test chooses: an interval measured from the real clock puts the claim
    hours away from the moment being asked about, and the assertion then measures the
    gap between the two clocks rather than the lease.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migration_url)
    try:
        async with engine.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE sched.org_sync_schedules "
                    "SET last_started_at = :started, last_finished_at = :finished "
                    "WHERE org_id = :o"
                ),
                {"started": started_at, "finished": finished_at, "o": str(org_id)},
            )
    finally:
        await engine.dispose()


class TestTheOrgLessIndex:
    async def test_creating_an_organisation_schedules_it(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """A trigger, not a settings page. An organisation nobody has configured still
        has to be synced, or the feature only works for tenants who went looking for it."""
        org_a, _ = two_orgs
        await _scope(conn, org_a)

        row = (await conn.execute(text("SELECT * FROM sched.read_schedule()"))).first()

        assert row is not None, "the AFTER INSERT trigger on orgs did not fire"
        assert (row.timezone, row.hour_local, row.enabled) == ("Asia/Kolkata", 1, True)

    async def test_the_default_is_one_in_the_morning(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        org_a, _ = two_orgs
        await _scope(conn, org_a)
        row = (await conn.execute(text("SELECT * FROM sched.read_schedule()"))).one()
        assert row.hour_local == 1


class TestPrivilegeIsTheBoundary:
    """There is no RLS here — the table holds an id, a zone and an hour, and no tenant
    content at all. What stops a request path reading another organisation's row is that
    it holds no privilege on the table whatsoever."""

    async def test_the_application_role_cannot_read_the_table(self, conn: AsyncConnection) -> None:
        with pytest.raises(DBAPIError) as refused:
            await conn.execute(text("SELECT count(*) FROM sched.org_sync_schedules"))
        assert "permission denied" in str(refused.value).lower()
        await conn.rollback()

    async def test_the_application_role_cannot_write_the_table(self, conn: AsyncConnection) -> None:
        with pytest.raises(DBAPIError) as refused:
            await conn.execute(text("UPDATE sched.org_sync_schedules SET enabled = false"))
        assert "permission denied" in str(refused.value).lower()
        await conn.rollback()

    async def test_a_read_takes_the_organisation_from_the_scope_not_an_argument(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """`read_schedule()` has no `org_id` parameter, so no call site can name a
        tenant — the rule `scoped_acl_principals` follows, for the same reason."""
        org_a, org_b = two_orgs
        await _scope(conn, org_a)
        await conn.execute(
            text("SELECT sched.write_schedule('Asia/Kolkata', CAST(4 AS smallint), true, NULL)")
        )

        await _scope(conn, org_b)
        theirs = (await conn.execute(text("SELECT * FROM sched.read_schedule()"))).one()

        assert theirs.timezone == "Asia/Kolkata", "org B must not see org A's schedule"
        assert theirs.hour_local == 1

    async def test_an_unscoped_session_reads_nothing(self, conn: AsyncConnection) -> None:
        await _clear_scope(conn)
        assert (await conn.execute(text("SELECT * FROM sched.read_schedule()"))).all() == []

    async def test_an_unscoped_session_cannot_write(self, conn: AsyncConnection) -> None:
        """Fail closed. Writing "the current organisation" with no current organisation
        would otherwise be an insert attributed to nobody."""
        await _clear_scope(conn)
        with pytest.raises(DBAPIError):
            await conn.execute(
                text("SELECT sched.write_schedule('UTC', CAST(1 AS smallint), true, NULL)")
            )
        await conn.rollback()


class TestTimezone:
    async def test_an_unknown_zone_is_refused_at_the_write(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """At the moment somebody types it, not at 01:00 inside a job nobody watches."""
        org_a, _ = two_orgs
        await _scope(conn, org_a)
        with pytest.raises(DBAPIError) as refused:
            await conn.execute(
                text("SELECT sched.write_schedule('Mars/Olympus', CAST(1 AS smallint), true, NULL)")
            )
        assert "timezone" in str(refused.value).lower()
        await conn.rollback()

    async def test_an_hour_outside_the_day_is_refused(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        org_a, _ = two_orgs
        await _scope(conn, org_a)
        with pytest.raises(DBAPIError):
            await conn.execute(
                text("SELECT sched.write_schedule('UTC', CAST(24 AS smallint), true, NULL)")
            )
        await conn.rollback()


class TestDueness:
    """The whole point of storing a zone: 01:00 has to mean 01:00 where the organisation
    is. A schedule tested only at UTC would pass with the timezone ignored."""

    async def _schedule(
        self, conn: AsyncConnection, org_id: uuid.UUID, *, timezone: str, hour: int
    ) -> None:
        await _scope(conn, org_id)
        await conn.execute(
            text("SELECT sched.write_schedule(:tz, CAST(:h AS smallint), true, NULL)"),
            {"tz": timezone, "h": hour},
        )

    async def _due(self, conn: AsyncConnection, org_id: uuid.UUID, moment: datetime) -> bool:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM sched.due_organisations(:t) WHERE org_id = :o"),
                {"t": moment, "o": str(org_id)},
            )
        ).scalar_one()
        return bool(count)

    async def test_due_at_the_local_hour_and_not_at_the_same_hour_utc(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        org_a, _ = two_orgs
        await self._schedule(conn, org_a, timezone="Asia/Kolkata", hour=1)

        assert await self._due(conn, org_a, datetime(2026, 9, 8, 1, 30, tzinfo=IST))
        assert not await self._due(conn, org_a, datetime(2026, 9, 8, 4, 30, tzinfo=IST))
        # 01:30 UTC is 07:00 in Kolkata. An implementation that ignored the zone would
        # fire here, which is the bug this asserts against.
        assert not await self._due(conn, org_a, datetime(2026, 9, 8, 1, 30, tzinfo=UTC))

    async def test_two_organisations_in_different_zones_are_due_at_different_instants(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        org_a, org_b = two_orgs
        await self._schedule(conn, org_a, timezone="Asia/Kolkata", hour=1)
        await self._schedule(conn, org_b, timezone="UTC", hour=1)

        indian_one_am = datetime(2026, 9, 8, 1, 30, tzinfo=IST)
        assert await self._due(conn, org_a, indian_one_am)
        assert not await self._due(conn, org_b, indian_one_am)

        utc_one_am = datetime(2026, 9, 8, 1, 30, tzinfo=UTC)
        assert await self._due(conn, org_b, utc_one_am)
        assert not await self._due(conn, org_a, utc_one_am)

    async def test_a_disabled_schedule_is_never_due(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        org_a, _ = two_orgs
        await _scope(conn, org_a)
        await conn.execute(
            text("SELECT sched.write_schedule('UTC', CAST(1 AS smallint), false, NULL)")
        )
        assert not await self._due(conn, org_a, datetime(2026, 9, 8, 1, 30, tzinfo=UTC))

    async def test_claiming_is_once_per_local_day(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The tick runs every fifteen minutes and an organisation is due for a whole
        hour, so without a claim a run would start four times over."""
        org_a, _ = two_orgs
        await self._schedule(conn, org_a, timezone="UTC", hour=1)

        first = (
            await conn.execute(text("SELECT sched.mark_started(:o)"), {"o": str(org_a)})
        ).scalar_one_or_none()
        second = (
            await conn.execute(text("SELECT sched.mark_started(:o)"), {"o": str(org_a)})
        ).scalar_one_or_none()

        assert first is True
        assert second is None, "a second tick in the same local day must not claim it"

    async def test_a_claimed_organisation_drops_out_of_the_due_list(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        org_a, _ = two_orgs
        await self._schedule(conn, org_a, timezone="UTC", hour=1)
        # Through the function, because the application role holds no privilege on the
        # table — which is the containment the class above asserts, and a direct UPDATE
        # here would fail for that reason rather than testing anything.
        await conn.execute(text("SELECT sched.mark_started(:o)"), {"o": str(org_a)})

        # Same local day as the claim, at the scheduled hour: due-ness is per local day,
        # so this is the moment a second tick would fire.
        moment = datetime.now(UTC).replace(hour=1, minute=30, second=0, microsecond=0)
        assert not await self._due(conn, org_a, moment)


class TestTheRunRecord:
    async def test_finishing_records_what_happened(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        org_a, _ = two_orgs
        await _scope(conn, org_a)
        await conn.execute(
            text("SELECT sched.mark_finished(:o, 'success', 3, 2)"), {"o": str(org_a)}
        )

        row = (await conn.execute(text("SELECT * FROM sched.read_schedule()"))).one()

        assert row.last_outcome == "success"
        assert (row.last_connections, row.last_enqueued) == (3, 2)
        assert row.last_finished_at is not None

    async def test_an_invented_outcome_is_refused(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        org_a, _ = two_orgs
        with pytest.raises(DBAPIError):
            await conn.execute(
                text("SELECT sched.mark_finished(:o, 'sort of', 0, 0)"), {"o": str(org_a)}
            )
        await conn.rollback()


class TestDaylightSaving:
    """The reason the column holds an IANA name rather than an offset.

    Every other zone in this file is DST-free, so an implementation that stored a fixed
    UTC offset would pass all of them. `Europe/London` is +00:00 in winter and +01:00 in
    summer, and on 2026-03-29 it jumps 01:00 straight to 02:00 — so hour 1 does not exist
    on the local clock that day at all.
    """

    LONDON = ZoneInfo("Europe/London")

    async def _due_at(self, conn: AsyncConnection, org_id: uuid.UUID, moment: datetime) -> bool:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM sched.due_organisations(:t) WHERE org_id = :o"),
                {"t": moment, "o": str(org_id)},
            )
        ).scalar_one()
        return bool(count)

    async def _schedule_london(self, conn: AsyncConnection, org_id: uuid.UUID) -> None:
        await _scope(conn, org_id)
        await conn.execute(
            text("SELECT sched.write_schedule('Europe/London', CAST(1 AS smallint), true, NULL)")
        )

    async def test_the_same_local_hour_is_a_different_instant_in_summer_and_winter(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """01:00 London is 01:00Z in January and 00:00Z in July. A stored offset gets one
        of these right and the other wrong, whichever value it holds."""
        org_a, _ = two_orgs
        await self._schedule_london(conn, org_a)

        assert await self._due_at(conn, org_a, datetime(2026, 1, 15, 1, 30, tzinfo=UTC))
        assert not await self._due_at(conn, org_a, datetime(2026, 1, 15, 0, 30, tzinfo=UTC))

        assert await self._due_at(conn, org_a, datetime(2026, 7, 15, 0, 30, tzinfo=UTC))
        assert not await self._due_at(conn, org_a, datetime(2026, 7, 15, 1, 30, tzinfo=UTC))

    async def test_an_hour_the_local_clock_skips_does_not_cost_the_night(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The defect this replaced: matching the hour LABEL meant that on a
        spring-forward day the organisation was silently invisible, all day, once a
        year. Resolved as an instant, the missing hour normalises forward."""
        org_a, _ = two_orgs
        await self._schedule_london(conn, org_a)

        # 2026-03-29: 01:00 GMT becomes 02:00 BST. There is no 01:xx local.
        assert not await self._due_at(conn, org_a, datetime(2026, 3, 29, 0, 30, tzinfo=UTC))
        assert await self._due_at(conn, org_a, datetime(2026, 3, 29, 1, 30, tzinfo=UTC))

    async def test_the_target_instant_is_what_the_api_promises(
        self, conn: AsyncConnection, two_orgs: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Two implementations of "the local clock reads 01:00" would disagree at exactly
        the moment it matters, and the console would promise a run that never happens."""
        from jutsu_api.sync_schedule import next_sync_at

        org_a, _ = two_orgs
        await self._schedule_london(conn, org_a)

        for probe in (
            datetime(2026, 3, 28, 12, 0, tzinfo=UTC),  # the day before the jump
            datetime(2026, 7, 14, 12, 0, tzinfo=UTC),  # ordinary summer
            datetime(2026, 1, 14, 12, 0, tzinfo=UTC),  # ordinary winter
        ):
            tomorrow = probe + timedelta(days=1)
            in_sql = (
                await conn.execute(
                    text(
                        "SELECT sched.target_instant(:t, CAST('Europe/London' AS text), "
                        "CAST(1 AS smallint))"
                    ),
                    {"t": tomorrow},
                )
            ).scalar_one()
            assert next_sync_at("Europe/London", 1, now=probe) == in_sql, probe


class TestTheClaimIsALease:
    async def test_a_claim_nobody_finished_is_takeable_after_an_hour(
        self,
        conn: AsyncConnection,
        two_orgs: tuple[uuid.UUID, uuid.UUID],
        migration_url: str,
    ) -> None:
        """A task killed between claiming and finishing — a job timeout, an OOM, a
        revision replaced mid-run — would otherwise hold the day's claim for ever, and
        the row would read as a run still in progress rather than as one that died."""
        org_a, _ = two_orgs
        await _scope(conn, org_a)
        await conn.execute(
            text("SELECT sched.write_schedule('UTC', CAST(1 AS smallint), true, NULL)")
        )

        assert (
            await conn.execute(text("SELECT sched.mark_started(:o)"), {"o": str(org_a)})
        ).scalar_one_or_none() is True
        # Immediately after, it is held.
        assert (
            await conn.execute(text("SELECT sched.mark_started(:o)"), {"o": str(org_a)})
        ).scalar_one_or_none() is None

        # The task died: the claim is old and nothing ever finished it.
        await conn.commit()
        await _age_claim(migration_url, org_a, minutes=90, finished=False)
        assert (
            await conn.execute(text("SELECT sched.mark_started(:o)"), {"o": str(org_a)})
        ).scalar_one_or_none() is True, "a dead claim must not hold the day for ever"

    async def test_a_finished_run_is_not_re_taken_within_the_day(
        self,
        conn: AsyncConnection,
        two_orgs: tuple[uuid.UUID, uuid.UUID],
        migration_url: str,
    ) -> None:
        """The lease must not turn into "run again every hour": a run that FINISHED is
        done for the local day, however long ago it was.

        **Anchored to today, not aged by an interval.** `mark_started` compares local
        DATES, so a claim placed ninety minutes before the real clock lands on
        *yesterday* whenever the suite runs before 01:30 UTC — the day has rolled over,
        the run is legitimately due again, and the test failed on a property it was
        never asserting. CI found it at 01:29 UTC; the same tree passed at 23:45. The
        sibling lease test above records this trap already: an interval measured from
        the real clock asserts a state that cannot occur. Midnight-plus-five is the
        earliest instant that is unambiguously *this* local day, which is also the
        strongest form of "however long ago".
        """
        org_a, _ = two_orgs
        await _scope(conn, org_a)
        await conn.execute(
            text("SELECT sched.write_schedule('UTC', CAST(1 AS smallint), true, NULL)")
        )
        await conn.commit()

        started = datetime.now(UTC).replace(hour=0, minute=5, second=0, microsecond=0)
        await _set_claim(
            migration_url, org_a, started_at=started, finished_at=started + timedelta(minutes=1)
        )

        assert (
            await conn.execute(text("SELECT sched.mark_started(:o)"), {"o": str(org_a)})
        ).scalar_one_or_none() is None


class TestTheBackfillReadsTheMembershipIndex:
    def test_it_never_reads_orgs(self) -> None:
        """The one property of this migration that cannot fail on a developer machine and
        can fail in production: the bootstrap role here is a superuser and reads `orgs`
        fine, while production's migration role is `postgres` with no `rolbypassrls` and
        reads zero rows from it. A backfill from `orgs` would seed twelve schedules
        locally and none in production — silently, since an INSERT-SELECT of nothing is
        not an error. Asserted against the source text because there is no environment
        here in which the wrong version fails.
        """
        migration = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src"
            / "jutsu_db"
            / "migrations"
            / "versions"
            / "0020_sync_schedule.py"
        ).read_text(encoding="utf-8")

        # The trigger body inserts one row too, so anchor on the SELECT: a backfill is the
        # only statement here that reads a list of organisations from somewhere.
        backfill = migration[migration.index("SELECT DISTINCT org_id FROM") :]
        backfill = backfill[: backfill.index("ON CONFLICT")]

        assert "auth.identity_memberships" in backfill
        assert "FROM orgs" not in migration


class TestTheLeaseIsReachableFromTheOnlyCaller:
    """`mark_started` has always had a takeover branch; nothing could reach it.

    `due_organisations` is the only thing that ever offers an organisation to that
    claim, and it excluded anything started today whether or not it finished. So a run
    killed between the claim and the finish — a job timeout, an OOM, a revision replaced
    mid-run — held the day's claim for ever: every remaining tick skipped the
    organisation, the row read as a run still in progress, and the takeover code was
    unreachable. The two predicates now say the same thing, and these tests are what
    stop them drifting apart again.
    """

    async def _due(self, conn: AsyncConnection, org_id: uuid.UUID, moment: datetime) -> bool:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM sched.due_organisations(:t) WHERE org_id = :o"),
                {"t": moment, "o": str(org_id)},
            )
        ).scalar_one()
        return bool(count)

    async def test_a_run_that_died_is_offered_again_within_the_hour_window(
        self,
        conn: AsyncConnection,
        two_orgs: tuple[uuid.UUID, uuid.UUID],
        migration_url: str,
    ) -> None:
        org_a, _ = two_orgs
        await _scope(conn, org_a)
        await conn.execute(
            text("SELECT sched.write_schedule('UTC', CAST(1 AS smallint), true, NULL)")
        )
        await conn.commit()

        # **The timeline this must survive is the one that actually happens.**
        #
        # The organisation is due from 01:00 to 02:00 and the job ticks every fifteen
        # minutes. The 01:00 tick claims it; the task dies at 01:05. A later tick in that
        # same window has to be able to take the claim over — and the window is what
        # bounds the opportunity, so a lease longer than the window can never fire.
        #
        # An earlier version of this test aged the claim by ninety minutes against a
        # 01:30 evaluation, which places the claim at 00:00 — a moment the organisation
        # was not due and could not have been claimed. It asserted a state that cannot
        # occur, and passed while the lease was unreachable in every state that can.
        claimed_at = datetime(2026, 9, 10, 1, 0, tzinfo=UTC)
        await _set_claim(migration_url, org_a, started_at=claimed_at, finished_at=None)

        # 01:10 — five minutes in. A second tick must not start the run again.
        assert not await self._due(conn, org_a, claimed_at + timedelta(minutes=10))

        # 01:30 — two ticks later, still inside the window, nothing ever finished.
        assert await self._due(conn, org_a, claimed_at + timedelta(minutes=30)), (
            "a dead claim must be retaken by a later tick INSIDE the window; a lease "
            "longer than the window is a branch that exists and never fires"
        )

        # 01:45 — the last tick of the window can still take it.
        assert await self._due(conn, org_a, claimed_at + timedelta(minutes=45))

    async def test_a_finished_run_is_still_done_for_the_day(
        self,
        conn: AsyncConnection,
        two_orgs: tuple[uuid.UUID, uuid.UUID],
        migration_url: str,
    ) -> None:
        """The lease must not turn into "run again every hour". A run that FINISHED is
        finished, however long ago it was."""
        org_a, _ = two_orgs
        await _scope(conn, org_a)
        await conn.execute(
            text("SELECT sched.write_schedule('UTC', CAST(1 AS smallint), true, NULL)")
        )
        await conn.commit()

        claimed_at = datetime(2026, 9, 10, 1, 0, tzinfo=UTC)
        await _set_claim(
            migration_url,
            org_a,
            started_at=claimed_at,
            finished_at=claimed_at + timedelta(seconds=30),
        )

        # Every remaining tick of the window sees a finished run and leaves it alone.
        # Without this the shortened lease would turn into "run again every 20 minutes".
        for minutes in (10, 30, 45, 59):
            assert not await self._due(conn, org_a, claimed_at + timedelta(minutes=minutes)), (
                f"a finished run must not be retaken {minutes} minutes later"
            )


class TestTheClockHasItsOwnPermission:
    async def test_the_four_roles_that_own_it_hold_it_and_nobody_else_does(
        self, conn: AsyncConnection
    ) -> None:
        """Owner, Super Admin, IT Admin and HR Admin — the roles the product names.

        Its own key rather than a use of `org:update`, which does not reach HR: widening
        that one would also have handed HR the organisation's profile and its connection
        policies. The runtime check reads these rows, not `rbac.py`, so the grant has to
        exist here or the route refuses everybody.
        """
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT role_key FROM role_permissions "
                        "WHERE permission_key = 'sync:schedule_manage' ORDER BY role_key"
                    )
                )
            )
            .scalars()
            .all()
        )

        assert set(rows) == {"owner", "super_admin", "it_admin", "hr_admin"}

    async def test_the_catalogue_describes_it(self, conn: AsyncConnection) -> None:
        description = (
            await conn.execute(
                text("SELECT description FROM permissions WHERE key = 'sync:schedule_manage'")
            )
        ).scalar_one_or_none()

        assert description, "a permission with no description is one nobody can review"
