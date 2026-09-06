# 0017 — Production background execution: a Cloud Tasks doorbell, a worker that scales from zero, no Redis

Status: accepted

Scope: how queued work runs in production — the transport that wakes the worker, the
worker service itself, its authorization, its follow-up rings, and what stays out
(`jutsu_core.doorbell`, `jutsu_worker.http`, `jutsu_worker.drain`, `deploy.yml`,
`docs/deploy.md` §9).

## Context

ADR 0012 made Postgres the queue: a job exists because a row exists, claims are leases,
attempts are bounded, and a lost wake-up costs latency rather than work. What woke the
worker was arq over Redis, which dev has (Compose) and production never did — the
runbook priced Memorystore as more than the rest of the deployment together and made the
reaper a scheduled Cloud Run job instead. The consequence was recorded honestly in
amendment A5 and ADR 0016: "Sync now" enqueued durable rows that nobody drained, and the
knowledge tabs, coverage and learning path were empty in production because extraction
never ran there.

Spec §5 already names the shape: "Cloud Tasks (prod) / arq + Redis (dev), behind one
interface". §20 lists Cloud Tasks queues and a Cloud Run worker. The code had the
interface — `ring_doorbell` in the API, `drain_org_jobs` in the worker — and one
transport.

## Decisions

### 1. Cloud Tasks is the production doorbell, and the interface does not change.

`jutsu_core.doorbell.CloudTasksDoorbell` creates one HTTP task per ring: a POST of
`{"org_id"}` to the worker's `/drain`, scheduled two seconds past the caller's commit,
signed with an OIDC token for the runtime service account. `jutsu_api.queue.ring_doorbell`
prefers it when `CLOUD_TASKS_QUEUE`, `CLOUD_TASKS_SERVICE_ACCOUNT` and `WORKER_DRAIN_URL`
are set and falls back to arq otherwise; call sites are untouched. A partial
configuration is refused at startup (`MisconfiguredDoorbell`) — a doorbell that silently
never rings is a queue that silently never moves, and the deploy's readiness check is
where that should fail.

### 2. Rings coalesce by name, inside a short window.

A task is named `{bucket}-{org}-{window}`; a burst inside one five-second window is one
dispatch, and `ALREADY_EXISTS` is success. The window is short on purpose: Cloud Tasks
tombstones a used name for about an hour after the task runs, so a name without a window
would ring once and then be refused for an hour.

### 3. The worker is a private Cloud Run service that scales from zero.

`jutsu_worker.http` is a two-route FastAPI app: `/healthz`, and `/drain`, which runs the
same `drain_org` the arq handler runs. Deployed `--no-allow-unauthenticated`,
`--concurrency 1`, `--min-instances 0`, `--timeout 600` (the drain bounds itself at 480 s
and the queue's dispatch deadline sits above both). Only the runtime service account
holds `run.invoker` on it, and that is the identity Cloud Tasks signs with — Cloud Run's
IAM check is the gate; the `X-CloudTasks-TaskName` check inside the app is the belt over
a mis-deploy that opened the service. Nothing runs or bills between doorbells, which is
the property the reaper job was created to keep, and the reason §20's
`min-instances=1` is amended (A6) rather than followed.

### 4. One follow-up decision, two transports.

`jutsu_worker.drain.drain_and_report` runs the drain and then decides: work still
claimable → ring again in five seconds; only retries with a future `next_attempt_at` →
ring after sixty. The arq handler rings Redis with that decision; the HTTP door rings
Cloud Tasks with it, addressed to the URL the request arrived on. A test holds both
dispatchers to the same function, so the queue cannot stall only in production.

### 5. More rings, all org-scoped.

The API rings on "Sync now" (as before), when an administrator opens the Jobs page, and
when a session is opened at sign-in. Each ring carries only the organisation the session
already held; the worker still cannot enumerate tenants (ADR 0012), so the residue —
an organisation nobody signs into and nobody syncs keeps its orphaned rows — stands, now
bounded by "until somebody from that organisation returns".

### 6. Not built here.

No Redis in production. No always-on worker. No nightly analysis agent (§13) — when it
lands it is a Cloud Scheduler-invoked job like the reaper, not a cron inside a process
that would then have to stay up. Cloud Tasks retry (10 attempts, 10 s–300 s backoff) is
the queue's; the job's own bounded attempts and dead-letter state are unchanged.

## Consequences

* Production drains: "Sync now" runs within seconds of the request, embeddings and
  extraction follow in the same drain, and the KT console's knowledge tabs fill for a
  tenant whose documents have been synced.
* Three new environment variables on the API, two on the worker; one queue and three IAM
  bindings created once, out of band, and recorded in `docs/deploy.md` §9.
* `google-cloud-tasks` joins `jutsu-core`'s dependencies, imported lazily.
* Cost at idle is unchanged: zero instances, an empty queue, no broker.
