# 0018 — Scheduled synchronisation: a clock that cannot read a tenant

Status: accepted

Scope: how connected providers are re-fetched without anyone clicking — the schedule, its
timezone, and the one question that blocked it: how a clock-driven process learns which
organisations exist (`sched` schema in migration 0020, `jutsu_worker.schedule`,
`GET`/`PUT /v1/orgs/current/sync-schedule`, the `jutsu-sync` Cloud Run job).

## Context

Everything downstream of a connector worked and nothing started it. `POST /v1/me/connections/{id}/sync`
enqueues a durable `connector.sync` row, ADR 0017's doorbell wakes a worker within seconds,
and the pipeline walks, masks, chunks, embeds and extracts. But the only things that ever
enqueued were a person pressing "Sync now", a sign-in, and an administrator opening the Jobs
page. A connection nobody touches is never re-read: `last_sync_at` simply stops advancing,
the corpus goes stale, and nothing says so. There was no cron, no scheduler, no nightly
anything — verified by search, not assumed.

The obvious implementation is forbidden here. A nightly job must ask "which organisations
are due", and **no role in this deployment can answer that**. `orgs` is `ENABLE` + `FORCE`
row-level security; `jutsu_app` sees zero rows without `app.current_org_id`; and production's
migration role is `postgres` with `rolsuper = false` and `rolbypassrls = false`, which sees
zero rows too — measured against production, where a `SELECT count(*) FROM orgs` returned 0.
ADR 0012 already refused the escape hatch: a `BYPASSRLS` service role "is the service-role
bypass the rules forbid", and a `SECURITY DEFINER` function over a FORCE-RLS table returns
zero rows with no error.

So the clock needed a tenant list that is not tenant data.

## Decisions

### 1. The schedule lives in an org-less index, outside RLS, holding no tenant content.

`sched.org_sync_schedules` is one row per organisation: `org_id`, an IANA `timezone`
(default `Asia/Kolkata`), an `hour_local` (default 1), `enabled`, and the outcome of the
last run. That is the whole table. It carries
no name, no address, no document, nothing a tenant would call theirs — the same shape and the
same justification as `auth.identity_memberships`, which this codebase already calls "the
org-less membership index". RLS is the wrong tool for a table with no tenant content to
protect; **privilege is**, exactly as the `auth` schema is contained today.

`jutsu_app` therefore holds **no table privilege at all** on it — only `EXECUTE` on the five
functions below. A compromised request path cannot read another organisation's schedule
because there is no statement it can write that reads the table.

### 2. A tenant's own reads and writes take the organisation from the GUC, never from an argument.

`sched.read_schedule()` and `sched.write_schedule(...)` take no `org_id`. They resolve it from
`app.current_org_id`, the same session GUC every RLS policy reads, and raise when it is unset.
This is the rule `scoped_acl_principals` already follows — "the organisation never comes from
a call site" — and it means the API physically cannot write another tenant's schedule even
with a forged request body.

### 3. `sched.due_organisations(now)` is the one deliberate cross-tenant read, and it returns three columns.

`(org_id, timezone, hour_local)` for enabled rows whose local hour has arrived and which have
not run yet today in their own zone. It is `SECURITY DEFINER`, executable by `jutsu_app`, and
called by exactly one caller — the scheduled job. That is the same trust shape as
`auth.reap_expired_registrations()`, which `jutsu_app` may also execute and which only the
reaper calls. It discloses that an organisation exists and when it wants to sync; it cannot
disclose anything about anyone in it.

This is the residue ADR 0012 left open, closed on the narrowest possible surface: not a
sweeper that can read tenant tables, but a clock that can read a list of clocks.

### 4. Due-ness is decided in Postgres, in the organisation's own zone.

```sql
WHERE enabled
  AND p_now >= sched.target_instant(p_now, timezone, hour_local)
  AND p_now <  sched.target_instant(p_now, timezone, hour_local) + INTERVAL '1 hour'
  AND (last_started_at IS NULL
       OR (last_started_at AT TIME ZONE timezone)::date < (p_now AT TIME ZONE timezone)::date)
```

`AT TIME ZONE` with an IANA name is DST-correct by construction, which a stored UTC offset
would not be — an organisation on `Asia/Kolkata` and one on `Europe/London` diverge twice a
year, and "01:00" has to keep meaning 01:00 for both. The job ticks every fifteen minutes, so
there are four chances inside the hour and `last_started_at` collapses them to one run per
local day.

The hour is resolved to an **instant** (`sched.target_instant`) rather than matched as a
wall-clock label, and that is not a detail: a chosen hour that a spring-forward transition
removes from the local clock never matches a label, so the organisation would be silently
invisible for that whole day. As an instant it normalises forward — 01:00 in a zone that
jumps 01:00 to 02:00 runs at 02:00 local. The API computes `next_sync_at` with the same
arithmetic, so the time the console promises and the time the clock keeps cannot diverge.

One consequence stated rather than discovered later: a scheduler outage lasting a full
hour costs that day's run, which is deliberate — firing at an arbitrary later hour is
worse than not firing, because these are provider quotas being spent.

The claim is a lease rather than a lock, for the reason `jobs.locked_until` is one: a task
killed between claiming and finishing would otherwise hold the day's claim for ever, and
the row would read as a run still in progress rather than as one that died. An unfinished
claim older than an hour is takeable by the next tick.

**That lease has to be written twice, and the second place is easy to forget.**
`mark_started` carries the takeover branch, but `due_organisations` is the only thing
that ever offers an organisation to it — so while that predicate excluded anything
started today, finished or not, the branch was unreachable from its only caller and a
killed run cost the whole local day in silence. The two predicates now say the same
thing, and two tests hold them together.

### 5. The row is created by a trigger, and backfilled from the membership index.

`orgs` gains an `AFTER INSERT` trigger that writes the default schedule, so a new organisation
is scheduled from the moment it exists rather than from the first time an administrator opens
a settings page. Existing organisations are backfilled in the migration from
`SELECT DISTINCT org_id FROM auth.identity_memberships` — **not** from `orgs`, which the
migration role cannot read (see Context). An organisation with no members has nothing to sync.

### 6. The job enqueues and rings; it does not fetch.

For each due organisation the job opens an ordinary `org_session(org_id)` — RLS on, app role,
no bypass — enqueues one `connector.sync` per `connected` connection using the same
idempotency key `sync_now` uses, and rings ADR 0017's doorbell. Everything after that is the
existing pipeline, unchanged. The job is therefore small, has no provider credentials of its
own, and cannot ingest anything: a bug in the clock can enqueue duplicate work, which the
idempotency key refuses, and nothing else.

It is a Cloud Scheduler → Cloud Run job, like the reaper (ADR 0017 §3 and `docs/deploy.md` §8),
because a scheduler living inside a process means a container that never idles.

### 7. Who may change it.

`GET` needs `integration:self_manage`, which every role holds: an employee who can connect
a tool is entitled to know when it will be read again, and that is what their integrations
page shows. The run history below it — `last_connections` counts the whole tenant — is
redacted for anyone without `org:read`, the same line `GET /v1/orgs/current` draws around
member counts.

`PUT` needs **`sync:schedule_manage`**, a permission this migration introduces, held by
Owner, Super Admin, IT Admin and HR Admin. It is not `org:update`, and the difference is
the point: `org:update` does not reach HR, and HR is a role that feels a stale corpus
first — a handover assembled from a fortnight-old index is the failure they notice.
Adding HR to `org:update` would have granted them the organisation's profile and its
connection policies at the same time, which nobody asked for. §17's rule is that a
permission names a *feature*; this one names the clock and nothing else. Manual "Sync now"
on one's own connection is unchanged and stays available to every member.

## Consequences

* A connected provider is re-read every night without anyone touching the product, and the
  ADR 0012 orphaned-job residue now has a nightly sweep for every organisation with a member.
* One new schema, one table, five functions, one trigger; no new role, no `BYPASSRLS`, no
  cross-tenant table read anywhere.
* An organisation's timezone is now a stored fact. Nothing else reads it yet; when a surface
  needs to render a local time, this is the column.
* The scheduler is a fourth place that enqueues sync work. All four go through
  `enqueue_job` with the same idempotency key, so they cannot duplicate each other.
