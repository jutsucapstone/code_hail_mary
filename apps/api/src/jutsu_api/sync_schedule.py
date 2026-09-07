"""When an organisation's connected providers are re-read, and who may change it.

The schedule lives outside row-level security because it holds no tenant content — an
id, a zone, an hour, a flag (ADR 0018). What contains it is privilege: `jutsu_app` has no
table access at all, only `EXECUTE` on two functions that take the organisation from
`app.current_org_id` rather than from an argument. So there is deliberately no `org_id`
parameter in this module: passing one is the mistake the design removes.

`next_sync_at` is computed here rather than stored. A stored timestamp would be wrong
twice a year — the schedule is "01:00 local", and what that means in UTC moves with the
zone's daylight-saving rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jutsu_core.errors import ValidationFailed
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SyncSchedule", "next_sync_at", "read_sync_schedule", "write_sync_schedule"]

#: What an organisation gets before anyone chooses: 01:00 `Asia/Kolkata`, on.
#:
#: Both halves are the product's documented default and both are only a default — the
#: settings page sets either, and the clock resolves whatever is stored with
#: `AT TIME ZONE`, so an organisation in another zone is served correctly the moment it
#: says so. What this value decides is where a tenant that never opens that page lands:
#: `UTC` put its first sync at 06:30 local for this deployment's own users, inside the
#: working day, spending provider quota against the accounts they were using. It must
#: match `sched.org_sync_schedules.timezone`'s column default, which is what a row
#: created by the trigger actually gets; `test_the_defaults_agree_with_the_column` pins
#: the two together.
DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_HOUR_LOCAL = 1


@dataclass(frozen=True, slots=True)
class SyncSchedule:
    timezone: str
    hour_local: int
    enabled: bool
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_outcome: str | None
    last_connections: int
    last_enqueued: int

    @property
    def next_sync_at(self) -> datetime | None:
        """None when disabled — there is no next one, and a date there would be a lie."""
        return next_sync_at(self.timezone, self.hour_local) if self.enabled else None


def _zone(name: str) -> ZoneInfo:
    """The zone, or a refusal a person can act on.

    Validated here as well as in the database so the error names the field. Postgres
    raises `invalid_parameter_value` for an unknown zone, which would otherwise surface
    as a 500 for what is a typed-in mistake.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValidationFailed(f"'{name}' is not a known time zone.") from exc


def next_sync_at(timezone: str, hour_local: int, *, now: datetime | None = None) -> datetime:
    """The next moment the local clock reads `hour_local`:00 in `timezone`.

    The same arithmetic as `sched.target_instant`, deliberately: a naive local date plus
    the hour, then localised. Two independent implementations of "the local clock reads
    01:00" disagree at exactly the moment it matters — a spring-forward gap, where this
    one normalises the missing hour forward and a wall-clock comparison finds nothing —
    and the console would then promise a run the clock will never make.

    Built from a naive date rather than by adding 24 hours to an aware instant, because
    across a daylight-saving boundary that lands an hour early or late, and this is the
    number a person checks against their own wall clock.
    """
    zone = _zone(timezone)
    moment = (now or datetime.now(UTC)).astimezone(zone)
    target = datetime.combine(moment.date(), time(hour=hour_local), tzinfo=zone)
    if target <= moment:
        tomorrow = moment.date() + timedelta(days=1)
        target = datetime.combine(tomorrow, time(hour=hour_local), tzinfo=zone)
    return target.astimezone(UTC)


async def read_sync_schedule(session: AsyncSession) -> SyncSchedule:
    """This organisation's schedule, from the session's own scope.

    A missing row means an organisation created before migration 0020 that has no member
    — which cannot hold a session, so cannot reach this — and the documented defaults are
    the honest answer for it rather than a 404 about a thing that has no absence.
    """
    row = (
        await session.execute(
            text(
                "SELECT timezone, hour_local, enabled, last_started_at, last_finished_at, "
                "last_outcome, last_connections, last_enqueued FROM sched.read_schedule()"
            )
        )
    ).first()
    if row is None:
        return SyncSchedule(
            timezone=DEFAULT_TIMEZONE,
            hour_local=DEFAULT_HOUR_LOCAL,
            enabled=True,
            last_started_at=None,
            last_finished_at=None,
            last_outcome=None,
            last_connections=0,
            last_enqueued=0,
        )
    return SyncSchedule(
        timezone=str(row.timezone),
        hour_local=int(row.hour_local),
        enabled=bool(row.enabled),
        last_started_at=row.last_started_at,
        last_finished_at=row.last_finished_at,
        last_outcome=row.last_outcome,
        last_connections=int(row.last_connections),
        last_enqueued=int(row.last_enqueued),
    )


async def write_sync_schedule(
    session: AsyncSession,
    *,
    actor_id: UUID,
    timezone: str,
    hour_local: int,
    enabled: bool,
) -> SyncSchedule:
    """Set the schedule for the session's organisation. Validates the zone first."""
    _zone(timezone)
    await session.execute(
        text("SELECT sched.write_schedule(:tz, CAST(:hour AS smallint), :enabled, :actor)"),
        {"tz": timezone, "hour": hour_local, "enabled": enabled, "actor": str(actor_id)},
    )
    return await read_sync_schedule(session)
