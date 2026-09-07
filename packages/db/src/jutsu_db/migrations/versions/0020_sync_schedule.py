"""The nightly sync schedule: an org-less index a clock may read (ADR 0018).

Everything downstream of a connector worked and nothing started it. A nightly job has to
ask "which organisations are due", and no role here can answer that from `orgs`: it is
`ENABLE` + `FORCE` row-level security, `jutsu_app` sees nothing without the GUC, and
production's migration role is `postgres` with neither `rolsuper` nor `rolbypassrls` — it
also sees nothing. ADR 0012 refuses the escape hatch.

So the clock reads a table that is not tenant data. `sched.org_sync_schedules` holds an
organisation id, a timezone, an hour and a flag, and nothing else — the same shape as
`auth.identity_memberships`, "the org-less membership index". RLS protects tenant content;
this has none. **Privilege is what contains it**: `jutsu_app` gets `USAGE` on the schema
and `EXECUTE` on five functions, and no table privilege whatsoever, so no statement a
request path can write reads another organisation's row.

The backfill reads `auth.identity_memberships`, NOT `orgs`. That is not a stylistic
choice — reading `orgs` here returns zero rows in production and twelve in dev, where the
bootstrap role happens to be a superuser, which is precisely the kind of divergence that
ships broken.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

#: The clock is its own feature, and these four roles own it.
SCHEDULE_PERMISSION = "sync:schedule_manage"
SCHEDULE_ROLES = ("owner", "super_admin", "it_admin", "hr_admin")

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS sched")

    # The default is 01:00 `Asia/Kolkata`, which is the product's documented default and
    # the timezone this deployment serves. It is a default, not a policy: an
    # organisation anywhere else sets its own zone on the settings page and the clock
    # resolves it with `AT TIME ZONE`. What matters is that a tenant which never opens
    # that page still syncs in the middle of ITS night — `UTC` here would have put a
    # first sync at 06:30 local, in the working day, spending provider quota against
    # people who were using the same accounts.
    #
    # No `org_id` policy and no RLS, deliberately: there is no tenant content to protect,
    # and a policy here would be a decoration that implies one. The FK to `orgs` is
    # checked by the system, which is not subject to RLS, so it holds even though nothing
    # in this schema can read `orgs`.
    op.execute(
        """
        CREATE TABLE sched.org_sync_schedules (
            org_id            uuid PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
            timezone          text NOT NULL DEFAULT 'Asia/Kolkata',
            hour_local        smallint NOT NULL DEFAULT 1,
            enabled           boolean NOT NULL DEFAULT true,
            last_started_at   timestamptz,
            last_finished_at  timestamptz,
            last_outcome      text,
            last_connections  integer NOT NULL DEFAULT 0,
            last_enqueued     integer NOT NULL DEFAULT 0,
            updated_at        timestamptz NOT NULL DEFAULT now(),
            updated_by        uuid,
            CONSTRAINT ck_org_sync_hour CHECK (hour_local BETWEEN 0 AND 23),
            CONSTRAINT ck_org_sync_outcome
                CHECK (last_outcome IS NULL OR last_outcome IN ('success', 'partial', 'failed'))
        )
        """
    )

    # The timezone cannot be a CHECK — validating an IANA name means consulting
    # `pg_timezone_names`, which is not immutable. The writer below validates it instead,
    # by converting through it: an unknown zone raises there, at the moment somebody sets
    # it, rather than at 01:00 inside a job nobody is watching.
    op.execute(
        """
        CREATE FUNCTION sched.assert_timezone(p_timezone text)
        RETURNS void
        LANGUAGE plpgsql IMMUTABLE AS $fn$
        BEGIN
            PERFORM TIMESTAMPTZ '2026-01-01 00:00:00+00' AT TIME ZONE p_timezone;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'unknown timezone: %', p_timezone
                USING ERRCODE = 'invalid_parameter_value';
        END;
        $fn$;
        """
    )

    # Today's scheduled moment, as an instant. Shared by `due_organisations` and — via
    # the same arithmetic in Python — by the `next_sync_at` the console shows, so the two
    # cannot disagree about a time the product has promised somebody.
    op.execute(
        """
        CREATE FUNCTION sched.target_instant(p_now timestamptz, p_timezone text, p_hour smallint)
        RETURNS timestamptz
        LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $fn$
          SELECT (((p_now AT TIME ZONE p_timezone)::date
                   + make_interval(hours => p_hour)) AT TIME ZONE p_timezone);
        $fn$;
        """
    )

    # The organisation comes from the session GUC, never from an argument. That is the
    # rule `scoped_acl_principals` follows for the same reason: a parameter here would let
    # a call site name a tenant, and then a forged request body could write somebody
    # else's schedule.
    op.execute(
        """
        CREATE FUNCTION sched.read_schedule()
        RETURNS TABLE (
            timezone text, hour_local smallint, enabled boolean,
            last_started_at timestamptz, last_finished_at timestamptz,
            last_outcome text, last_connections integer, last_enqueued integer
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, sched AS $fn$
          SELECT s.timezone, s.hour_local, s.enabled, s.last_started_at, s.last_finished_at,
                 s.last_outcome, s.last_connections, s.last_enqueued
            FROM sched.org_sync_schedules s
           WHERE s.org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid;
        $fn$;
        """
    )

    # Upsert rather than update: an organisation created before this migration, or one
    # whose trigger row was removed, must still be able to set a schedule.
    op.execute(
        """
        CREATE FUNCTION sched.write_schedule(
            p_timezone text, p_hour smallint, p_enabled boolean, p_actor uuid
        )
        RETURNS void
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, sched AS $fn$
        DECLARE
            v_org uuid := NULLIF(current_setting('app.current_org_id', true), '')::uuid;
        BEGIN
            IF v_org IS NULL THEN
                RAISE EXCEPTION 'no organisation in scope'
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            PERFORM sched.assert_timezone(p_timezone);
            INSERT INTO sched.org_sync_schedules (org_id, timezone, hour_local, enabled, updated_by)
            VALUES (v_org, p_timezone, p_hour, p_enabled, p_actor)
            ON CONFLICT (org_id) DO UPDATE
               SET timezone = EXCLUDED.timezone,
                   hour_local = EXCLUDED.hour_local,
                   enabled = EXCLUDED.enabled,
                   updated_by = EXCLUDED.updated_by,
                   updated_at = now();
        END;
        $fn$;
        """
    )

    # The one deliberate cross-tenant read in the system, and it returns three columns:
    # who is due, in which zone, at which hour. No name, no address, no content. Same
    # trust shape as `auth.reap_expired_registrations()` — `jutsu_app` may execute it and
    # exactly one caller does.
    #
    # **The schedule is an instant, not an hour label.** Matching
    # `EXTRACT(HOUR FROM (p_now AT TIME ZONE tz)) = hour_local` looks equivalent and is
    # not: at a spring-forward transition the chosen hour does not exist on the local
    # clock, so the label never matches and the organisation is silently invisible for
    # that entire day. Building today's target as a naive local timestamp and converting
    # it back with `AT TIME ZONE` normalises a nonexistent wall time forward instead —
    # 01:00 in a zone that jumps 01:00→02:00 becomes 02:00 local, which is a real instant.
    # A stored UTC offset would be wrong for half of every year, which is why the column
    # holds an IANA name.
    #
    # The hour-wide window keeps the promise the runbook makes: four ticks to catch it,
    # and an outage longer than that costs the day rather than firing at an arbitrary
    # hour — these are provider quotas being spent.
    #
    # **The third disjunct is the lease, and it has to be here as well as in
    # `mark_started`.** `mark_started` was written to let the next tick take over an
    # unfinished claim older than an hour, which is what stops a run killed between the
    # claim and the finish from holding the day for ever. But this function is the only
    # thing that ever offers an organisation to that claim, and it excluded anything
    # started today whether or not it finished — so the takeover branch could not be
    # reached from the only caller, and a job killed by a timeout or a replaced revision
    # cost the whole local day, silently. The two predicates now say the same thing.
    op.execute(
        """
        CREATE FUNCTION sched.due_organisations(p_now timestamptz)
        RETURNS TABLE (org_id uuid, timezone text, hour_local smallint)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, sched AS $fn$
          SELECT s.org_id, s.timezone, s.hour_local
            FROM sched.org_sync_schedules s
           WHERE s.enabled
             AND p_now >= sched.target_instant(p_now, s.timezone, s.hour_local)
             AND p_now < sched.target_instant(p_now, s.timezone, s.hour_local)
                         + INTERVAL '1 hour'
             AND (
                   s.last_started_at IS NULL
                   OR (s.last_started_at AT TIME ZONE s.timezone)::date
                      < (p_now AT TIME ZONE s.timezone)::date
                   OR (
                        (s.last_finished_at IS NULL
                         OR s.last_finished_at < s.last_started_at)
                        AND s.last_started_at < p_now - INTERVAL '1 hour'
                      )
                 )
           ORDER BY s.org_id;
        $fn$;
        """
    )

    # Claimed before the work, like every other claim in this system: the run is marked
    # started so a second tick inside the same hour finds it already taken, whatever
    # happens next.
    #
    # **And the claim is a lease, not a lock**, for the reason `jobs.locked_until` is one.
    # A task killed between the claim and the finish — a ten-minute job timeout, an OOM, a
    # revision replaced mid-run — would otherwise hold today's claim for ever: every
    # remaining tick would skip the organisation, and the row would read as a run still in
    # progress rather than as one that died. An unfinished claim older than an hour is
    # therefore takeable, which the next tick does.
    op.execute(
        """
        CREATE FUNCTION sched.mark_started(p_org_id uuid)
        RETURNS boolean
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, sched AS $fn$
          UPDATE sched.org_sync_schedules
             SET last_started_at = now(), last_outcome = NULL
           WHERE org_id = p_org_id
             AND (
                   last_started_at IS NULL
                   OR (last_started_at AT TIME ZONE timezone)::date
                      < (now() AT TIME ZONE timezone)::date
                   OR (
                        (last_finished_at IS NULL OR last_finished_at < last_started_at)
                        AND last_started_at < now() - INTERVAL '1 hour'
                      )
                 )
          RETURNING true;
        $fn$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION sched.mark_finished(
            p_org_id uuid, p_outcome text, p_connections integer, p_enqueued integer
        )
        RETURNS void
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, sched AS $fn$
          UPDATE sched.org_sync_schedules
             SET last_finished_at = now(), last_outcome = p_outcome,
                 last_connections = p_connections, last_enqueued = p_enqueued
           WHERE org_id = p_org_id;
        $fn$;
        """
    )

    # A new organisation is scheduled from the moment it exists, not from the first time
    # somebody opens a settings page. SECURITY DEFINER because the inserting session is
    # `jutsu_app`, which holds no privilege on this table at all.
    op.execute(
        """
        CREATE FUNCTION sched.register_organisation()
        RETURNS trigger
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, sched AS $fn$
        BEGIN
            INSERT INTO sched.org_sync_schedules (org_id)
            VALUES (NEW.id)
            ON CONFLICT (org_id) DO NOTHING;
            RETURN NEW;
        END;
        $fn$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER orgs_register_sync_schedule
        AFTER INSERT ON orgs
        FOR EACH ROW EXECUTE FUNCTION sched.register_organisation()
        """
    )

    # From the membership index, never from `orgs` — see the module docstring. An
    # organisation with no members has no connections either, so nothing is missed.
    op.execute(
        """
        INSERT INTO sched.org_sync_schedules (org_id)
        SELECT DISTINCT org_id FROM auth.identity_memberships
        ON CONFLICT (org_id) DO NOTHING
        """
    )

    # The schedule gets its own permission rather than borrowing `org:update`.
    #
    # The people who own this decision are the Owner, the Super Admin, the IT Admin and
    # the HR Admin — HR because onboarding and offboarding are when a stale corpus is
    # actually felt. `org:update` reaches only the first three, and widening it to HR
    # would also hand them the organisation's profile and its connection policies, which
    # is a bigger grant than anybody asked for. §17's rule is that a permission names a
    # *feature*: this one names the clock.
    op.bulk_insert(
        sa.table("permissions", sa.column("key", sa.String), sa.column("description", sa.Text)),
        [
            {
                "key": SCHEDULE_PERMISSION,
                "description": ("Set when the organisation's connected providers are re-read."),
            }
        ],
    )
    op.bulk_insert(
        sa.table(
            "role_permissions",
            sa.column("role_key", sa.String),
            sa.column("permission_key", sa.String),
        ),
        [{"role_key": role, "permission_key": SCHEDULE_PERMISSION} for role in SCHEDULE_ROLES],
    )

    # Privilege, not policy. `USAGE` on the schema and `EXECUTE` on the five functions;
    # the REVOKE is explicit because migration 0002 set default privileges that would
    # otherwise be inherited, and "no direct table access" is the containment.
    # One statement per call: asyncpg prepares each `op.execute`, and a prepared
    # statement cannot carry several commands.
    for statement in (
        "GRANT USAGE ON SCHEMA sched TO jutsu_app",
        "REVOKE ALL ON sched.org_sync_schedules FROM jutsu_app",
        "REVOKE ALL ON FUNCTION sched.assert_timezone(text) FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION sched.read_schedule() TO jutsu_app",
        "GRANT EXECUTE ON FUNCTION sched.write_schedule(text, smallint, boolean, uuid) TO jutsu_app",
        "GRANT EXECUTE ON FUNCTION sched.due_organisations(timestamptz) TO jutsu_app",
        "GRANT EXECUTE ON FUNCTION sched.mark_started(uuid) TO jutsu_app",
        "GRANT EXECUTE ON FUNCTION sched.mark_finished(uuid, text, integer, integer) TO jutsu_app",
    ):
        op.execute(statement)


def downgrade() -> None:
    # Explicit rather than relying on the FK cascade, so the reversal reads the same way
    # round as the application — the shape migration 0009 established.
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_key = :p").bindparams(
            p=SCHEDULE_PERMISSION
        )
    )
    op.execute(sa.text("DELETE FROM permissions WHERE key = :p").bindparams(p=SCHEDULE_PERMISSION))
    op.execute("DROP TRIGGER IF EXISTS orgs_register_sync_schedule ON orgs")
    for signature in (
        "sched.register_organisation()",
        "sched.mark_finished(uuid, text, integer, integer)",
        "sched.mark_started(uuid)",
        "sched.due_organisations(timestamptz)",
        "sched.target_instant(timestamptz, text, smallint)",
        "sched.write_schedule(text, smallint, boolean, uuid)",
        "sched.read_schedule()",
        "sched.assert_timezone(text)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("DROP TABLE IF EXISTS sched.org_sync_schedules")
    op.execute("DROP SCHEMA IF EXISTS sched")
