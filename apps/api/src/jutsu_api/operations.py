"""Operational read models: the audit trail, the job queue, sources and invitations.

Everything here is a *read* over tables other slices write, plus the two admin writes
that had permissions in the catalogue and no route behind them (renaming the
organisation, changing a member's role). The read queries share three rules:

* **No `org_id` predicate appears in any query.** Row-level security supplies it from
  the GUC, and adding a redundant one as "defence in depth" would mask a broken policy —
  the isolation test would still pass while the policy sat inert.
* **Keyset pagination, never OFFSET.** Every one of these tables is written to precisely
  while an administrator is looking at it; offset paging over a moving table skips and
  duplicates rows between pages.
* **Nothing here returns raw content.** Job payloads name documents and error strings can
  embed paths, so the list view carries the classified `failure_kind` and not the
  exception text (§4.9). The audit trail already stores opaque actor ids by design.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from jutsu_core.errors import (
    Conflict,
    NotFound,
    PermissionDenied,
    ServiceUnavailable,
    ValidationFailed,
)
from jutsu_core.rbac import Role, outranks
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "AuditPage",
    "InvitationPage",
    "JobPage",
    "JobStats",
    "OrgOverview",
    "SourceRow",
    "change_member_role",
    "list_audit",
    "list_invitations",
    "list_jobs",
    "list_sources",
    "org_overview",
    "read_job_stats",
    "rename_organisation",
    "request_source_sync",
]


# --------------------------------------------------------------------------- audit


@dataclass(frozen=True, slots=True)
class AuditRow:
    id: int
    #: Opaque actor. The JUTSU ID beside it is resolved for display when the actor is a
    #: user in this organisation; it is never an email (§4.9).
    actor_id: str | None
    actor_jutsu_id: str | None
    actor_type: str
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    ts: datetime
    correlation_id: str | None


@dataclass(frozen=True, slots=True)
class AuditPage:
    items: list[AuditRow]
    next_cursor: str | None


def _decode_audit_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        ts, last_id = cursor.split("|", 1)
        decoded_ts, decoded_id = datetime.fromisoformat(ts), int(last_id)
    except (ValueError, AttributeError) as exc:
        raise NotFound("That page does not exist.") from exc
    # `int()` happily reads a number Postgres cannot hold: past int8 the driver raises
    # at bind time and a crafted cursor becomes a 500. The trail's ids are positive
    # bigints, so anything outside that range is the same missing page as garbage.
    if not 0 <= decoded_id < 2**63:
        raise NotFound("That page does not exist.")
    return decoded_ts, decoded_id


async def list_audit(
    session: AsyncSession,
    *,
    limit: int,
    cursor: str | None,
    action: str | None = None,
    outcome: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> AuditPage:
    """The organisation's audit trail, newest first.

    `resource_id` narrows to one resource's history — a knowledge-transfer package's
    opens, claims and refusals, say. Server-side like every other filter here: a trail
    can be arbitrarily long, and a client filtering one page would report "no denials"
    while the denials sat on page two.

    The join to `users` resolves an actor to their JUTSU ID for display. It is guarded by
    a regex on the actor string because `actor_id` is `String(255)` by design — a system
    actor is not a UUID — and an unguarded `::uuid` cast would make one such row poison
    the whole page with a cast error.
    """
    bounded = max(1, min(limit, 100))

    filters = ["true"]
    params: dict[str, object] = {"limit": bounded + 1}

    if cursor:
        ts, last_id = _decode_audit_cursor(cursor)
        params["cursor_ts"] = ts
        params["cursor_id"] = last_id
        filters.append("(a.ts, a.id) < (:cursor_ts, :cursor_id)")
    if action:
        params["action"] = action
        filters.append("a.action = :action")
    if outcome:
        if outcome not in ("success", "denied", "failure"):
            raise ValidationFailed("outcome must be success, denied or failure.")
        params["outcome"] = outcome
        filters.append("a.outcome = :outcome")
    if resource_type:
        params["resource_type"] = resource_type
        filters.append("a.resource_type = :resource_type")
    if resource_id:
        params["resource_id"] = resource_id
        filters.append("a.resource_id = :resource_id")

    # S608: every joined fragment is a literal defined above; caller input is bound.
    rows = (
        await session.execute(
            text(
                "SELECT a.id, a.actor_id, a.actor_type, a.action, a.resource_type, "  # noqa: S608
                "a.resource_id, a.outcome, a.ts, a.correlation_id, u.jutsu_id AS actor_jutsu_id "
                "FROM audit_log a "
                "LEFT JOIN users u ON a.actor_id ~ "
                "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' "
                "AND u.id = a.actor_id::uuid "
                f"WHERE {' AND '.join(filters)} "
                "ORDER BY a.ts DESC, a.id DESC LIMIT :limit"
            ),
            params,
        )
    ).all()

    page = rows[:bounded]
    next_cursor = (
        f"{page[-1].ts.isoformat()}|{page[-1].id}" if len(rows) > bounded and page else None
    )
    return AuditPage(
        items=[
            AuditRow(
                id=r.id,
                actor_id=r.actor_id,
                actor_jutsu_id=r.actor_jutsu_id,
                actor_type=r.actor_type,
                action=r.action,
                resource_type=r.resource_type,
                resource_id=r.resource_id,
                outcome=r.outcome,
                ts=r.ts,
                correlation_id=r.correlation_id,
            )
            for r in page
        ],
        next_cursor=next_cursor,
    )


# --------------------------------------------------------------------------- jobs


@dataclass(frozen=True, slots=True)
class JobRow:
    id: UUID
    kind: str
    state: str
    attempts: int
    #: The classified failure, never the exception text — error strings can embed file
    #: paths and provider responses, which §4.9 keeps out of anything renderable.
    failure_kind: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class JobPage:
    items: list[JobRow]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class JobStats:
    by_state: dict[str, int]
    dead_letter: int
    #: Jobs whose most recent transition in the last 24h ended in a failed state. The
    #: dashboard's "failed jobs" figure — measured, not styled.
    failed_24h: int


def _decode_uuid_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        ts, last_id = cursor.split("|", 1)
        return datetime.fromisoformat(ts), UUID(last_id)
    except (ValueError, AttributeError) as exc:
        raise NotFound("That page does not exist.") from exc


async def list_jobs(
    session: AsyncSession,
    *,
    limit: int,
    cursor: str | None,
    state: str | None = None,
    kind: str | None = None,
) -> JobPage:
    bounded = max(1, min(limit, 100))

    filters = ["true"]
    params: dict[str, object] = {"limit": bounded + 1}

    if cursor:
        ts, last_id = _decode_uuid_cursor(cursor)
        params["cursor_ts"] = ts
        params["cursor_id"] = last_id
        filters.append("(j.updated_at, j.id) < (:cursor_ts, :cursor_id)")
    if state:
        params["state"] = state
        filters.append("j.state = :state")
    if kind:
        params["kind"] = kind
        filters.append("j.kind = :kind")

    rows = (
        await session.execute(
            text(
                "SELECT j.id, j.kind, j.state, j.attempts, j.failure_kind, "  # noqa: S608
                "j.created_at, j.updated_at FROM jobs j "
                f"WHERE {' AND '.join(filters)} "
                "ORDER BY j.updated_at DESC, j.id DESC LIMIT :limit"
            ),
            params,
        )
    ).all()

    page = rows[:bounded]
    next_cursor = (
        f"{page[-1].updated_at.isoformat()}|{page[-1].id}" if len(rows) > bounded and page else None
    )
    return JobPage(
        items=[
            JobRow(
                id=r.id,
                kind=r.kind,
                state=r.state,
                attempts=r.attempts,
                failure_kind=r.failure_kind,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in page
        ],
        next_cursor=next_cursor,
    )


async def read_job_stats(session: AsyncSession) -> JobStats:
    by_state_rows = (
        await session.execute(text("SELECT state, count(*) AS n FROM jobs GROUP BY state"))
    ).all()
    failed_24h = (
        await session.execute(
            text(
                "SELECT count(*) FROM jobs WHERE state IN ('failed', 'dead_letter') "
                "AND updated_at > now() - interval '24 hours'"
            )
        )
    ).scalar_one()
    by_state = {r.state: r.n for r in by_state_rows}
    return JobStats(
        by_state=by_state,
        dead_letter=by_state.get("dead_letter", 0),
        failed_24h=failed_24h,
    )


# --------------------------------------------------------------------------- sources


@dataclass(frozen=True, slots=True)
class SourceRow:
    id: UUID
    #: The ACL namespace, which is NOT the provider: all four Google products are
    #: `gmail` and the three Microsoft ones are `m365`. Kept because the ACL
    #: namespace is a real fact about the row, but never the thing to show alone.
    system: str
    #: The provider registry id from `config_json->>'provider'` — `google_drive`,
    #: `gmail`, `jira` — or None for a local source, which has no provider. Without
    #: it an administrator looking at four rows reading "gmail" cannot tell which is
    #: Drive and which is Calendar, and a Re-sync button beside them names the wrong
    #: thing.
    provider: str | None
    #: Whose account this source reads, as the provider labelled it at connect time
    #: (an address or a login). None for a local source or a connection since
    #: deleted. It is what distinguishes two rows for the same provider.
    account_label: str | None
    status: str
    last_sync_at: datetime | None
    #: Current document versions only — superseded versions are history, not inventory.
    document_count: int
    #: Ingestion jobs for this source's documents, counted by outcome. Real pipeline
    #: telemetry (§11): pending + running are in flight, completed made it through
    #: fetch→mask→chunk→persist, failed/dead_letter did not.
    jobs_pending: int
    jobs_completed: int
    jobs_failed: int
    #: The last walk's own counters, written by the worker in the same transaction as
    #: the cursor advance. Empty until a walk has run.
    last_walk: dict[str, int]


async def list_sources(session: AsyncSession) -> list[SourceRow]:
    """Every knowledge source in the organisation, with what it has produced.

    No pagination: a source is a configured connector, and an organisation has a handful
    of them, not thousands. The day that stops being true this grows a cursor.
    """
    rows = (
        await session.execute(
            text(
                "SELECT s.id, s.system, s.status, s.last_sync_at, s.stats_json, "
                "s.config_json->>'provider' AS provider, c.account_label, "
                "count(d.id) FILTER (WHERE d.superseded_by IS NULL) AS document_count, "
                # Document jobs carry deterministic keys prefixed by their source, which
                # is what makes this join possible without a source_id column on jobs.
                "(SELECT count(*) FROM jobs j WHERE j.idempotency_key LIKE "
                " 'ingest.document:' || s.org_id || ':' || s.id || ':%' "
                " AND j.state NOT IN ('completed', 'failed', 'dead_letter')) AS jobs_pending, "
                "(SELECT count(*) FROM jobs j WHERE j.idempotency_key LIKE "
                " 'ingest.document:' || s.org_id || ':' || s.id || ':%' "
                " AND j.state = 'completed') AS jobs_completed, "
                "(SELECT count(*) FROM jobs j WHERE j.idempotency_key LIKE "
                " 'ingest.document:' || s.org_id || ':' || s.id || ':%' "
                " AND j.state IN ('failed', 'dead_letter')) AS jobs_failed "
                "FROM sources s LEFT JOIN documents d ON d.source_id = s.id "
                "LEFT JOIN connections c ON c.id = (s.config_json->>'connection_id')::uuid "
                "GROUP BY s.id, s.system, s.status, s.last_sync_at, s.stats_json, "
                "c.account_label "
                "ORDER BY s.system, s.id"
            )
        )
    ).all()
    return [
        SourceRow(
            id=r.id,
            system=r.system,
            provider=r.provider,
            account_label=r.account_label,
            status=r.status,
            last_sync_at=r.last_sync_at,
            document_count=r.document_count,
            jobs_pending=r.jobs_pending,
            jobs_completed=r.jobs_completed,
            jobs_failed=r.jobs_failed,
            last_walk=(r.stats_json or {}).get("last_walk", {}),
        )
        for r in rows
    ]


#: The worker's `source_job_key` — one walk per source, org-qualified. Duplicated as a
#: literal rather than imported because `apps/api` and `apps/worker` are separate
#: applications and neither may depend on the other; `test_the_key_is_the_workers_own`
#: imports the worker's function and asserts the two spell the same string, so a change
#: on either side fails a test instead of quietly enqueueing a row nothing claims.
_SOURCE_JOB_KIND = "ingest.source"


async def request_source_sync(
    session: AsyncSession, *, org_id: UUID, actor_user_id: UUID, source_id: UUID
) -> UUID:
    """Queue a walk of one knowledge source, and return the id of the row that will run it.

    An administrator watching a stalled source could read its counters and do nothing
    about them. This is the act: a real row in the durable queue the worker already
    drains (`ingest.source`), followed by a doorbell at the call site. Nothing here
    fetches, and nothing here touches `sources.status` — the worker owns that column, and
    writing "syncing" from the API would put a state on the page that nothing ever clears
    (§4.11: no faked state behind a surface).

    Three properties, all inherited from `connectors.sync_now`:

    * **A source in another tenant is a 404, never a 403.** The lookup carries no
      `org_id` predicate: row-level security supplies the tenant, so another
      organisation's id simply finds nothing, and the refusal cannot distinguish "not
      yours" from "not real". A 403 here would confirm that the id exists.
    * **One pending walk per source.** The idempotency key is the source's identity, so
      a second click while a walk is queued or running returns the id of the job already
      doing the work rather than enqueueing a rival that would race it on the cursor.
    * **A finished walk is reopened, never duplicated.** Including a failed one — the
      infinite-retry loop that makes `reopen_completed_job` refuse failures cannot happen
      when a person's click is the loop, and an administrator who has fixed the source
      needs some way to say so.
    """
    exists = (
        await session.execute(
            text("SELECT 1 FROM sources WHERE id = :id"),
            {"id": source_id},
        )
    ).scalar_one_or_none()
    if exists is None:
        raise NotFound("That knowledge source was not found.")

    key = f"{_SOURCE_JOB_KIND}:{org_id}:{source_id}"
    inserted = (
        await session.execute(
            text(
                "INSERT INTO jobs (id, org_id, kind, state, idempotency_key, payload_json) "
                "VALUES (:id, :org, :kind, 'pending', :key, cast(:payload AS jsonb)) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id"
            ),
            {
                "id": uuid4(),
                "org": str(org_id),
                "kind": _SOURCE_JOB_KIND,
                "key": key,
                "payload": json.dumps({"source_id": str(source_id)}),
            },
        )
    ).first()
    if inserted is not None:
        job_id = UUID(str(inserted.id))
    else:
        reopened = (
            await session.execute(
                text(
                    "UPDATE jobs SET state = 'pending', attempts = 0, locked_until = NULL, "
                    "next_attempt_at = NULL, error = NULL, failure_kind = NULL, "
                    "updated_at = now() "
                    "WHERE idempotency_key = :key "
                    "AND state IN ('completed', 'failed', 'dead_letter') "
                    "RETURNING id"
                ),
                {"key": key},
            )
        ).first()
        if reopened is not None:
            job_id = UUID(str(reopened.id))
        else:
            # Queued or in flight: that job IS this re-sync, so name it rather than
            # pretending a second one was created.
            existing = (
                await session.execute(
                    text("SELECT id FROM jobs WHERE idempotency_key = :key"), {"key": key}
                )
            ).first()
            if existing is None:  # pragma: no cover - insert/update/select race
                raise ServiceUnavailable("The sync queue is briefly contended. Try again.")
            job_id = UUID(str(existing.id))

    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', 'source.sync_requested', 'source', :rid, 'success')"
        ),
        {"org": str(org_id), "actor": str(actor_user_id), "rid": str(source_id)},
    )
    return job_id


# ----------------------------------------------------------------------- invitations


@dataclass(frozen=True, slots=True)
class InvitationRow:
    id: UUID
    email: str
    role: Role
    #: Derived, in one place, so the list and any future filter cannot disagree about
    #: what "pending" means.
    status: str
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class InvitationPage:
    items: list[InvitationRow]
    next_cursor: str | None


def _invitation_status(row: object) -> str:
    if getattr(row, "revoked_at", None) is not None:
        return "revoked"
    if getattr(row, "accepted_at", None) is not None:
        return "accepted"
    expires_at: datetime = row.expires_at  # type: ignore[attr-defined]
    now: datetime = row.now  # type: ignore[attr-defined]
    return "expired" if expires_at <= now else "pending"


async def list_invitations(
    session: AsyncSession, *, limit: int, cursor: str | None
) -> InvitationPage:
    """Outstanding and settled invitations, newest first.

    The addressee's email is returned deliberately: the caller holds `member:invite`, and
    an invitation *is* an email address — there is no way to render "who was invited"
    without it. `now()` rides along from the database so expiry is judged against the
    same clock that wrote `expires_at`.
    """
    bounded = max(1, min(limit, 100))

    filters = ["true"]
    params: dict[str, object] = {"limit": bounded + 1}

    if cursor:
        ts, last_id = _decode_uuid_cursor(cursor)
        params["cursor_ts"] = ts
        params["cursor_id"] = last_id
        filters.append("(i.created_at, i.id) < (:cursor_ts, :cursor_id)")

    rows = (
        await session.execute(
            text(
                "SELECT i.id, i.email, i.role_key, i.created_at, i.expires_at, "  # noqa: S608
                "i.accepted_at, i.revoked_at, now() AS now FROM invitations i "
                f"WHERE {' AND '.join(filters)} "
                "ORDER BY i.created_at DESC, i.id DESC LIMIT :limit"
            ),
            params,
        )
    ).all()

    page = rows[:bounded]
    next_cursor = (
        f"{page[-1].created_at.isoformat()}|{page[-1].id}" if len(rows) > bounded and page else None
    )
    return InvitationPage(
        items=[
            InvitationRow(
                id=r.id,
                email=r.email,
                role=Role(r.role_key),
                status=_invitation_status(r),
                created_at=r.created_at,
                expires_at=r.expires_at,
                accepted_at=r.accepted_at,
                revoked_at=r.revoked_at,
            )
            for r in page
        ],
        next_cursor=next_cursor,
    )


# ----------------------------------------------------------------- role assignment


async def change_member_role(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    actor_role: Role,
    target_user_id: UUID,
    new_role: Role,
    org_id: UUID,
) -> Role:
    """Assign a member a different role, under the escalation rules.

    Three refusals, in order of importance:

    * **Never your own role.** Not a rank question — an Owner outranks everyone and could
      still not do this. Changing your own role is either self-promotion (the escalation
      this module exists to prevent) or self-demotion by accident, and an organisation
      whose only Owner demoted themselves has no one left who can undo it.
    * **The actor must outrank the target's current role**, strictly, so peers cannot
      act on each other (an HR Admin cannot touch an IT Admin).
    * **The actor must outrank the role being granted**, strictly, so nobody can promote
      anyone to their own level — which also means `owner` is structurally unassignable
      here, because nothing outranks it. Ownership transfer is a different operation with
      different ceremony, not a role change.
    """
    if target_user_id == actor_user_id:
        raise PermissionDenied("You cannot change your own role.")

    current = (
        await session.execute(
            text("SELECT role_key FROM user_roles WHERE user_id = :uid"),
            {"uid": target_user_id},
        )
    ).scalar_one_or_none()
    if current is None:
        # Either no such user in this organisation (RLS returned nothing) or they have
        # not accepted their invitation yet, in which case the role to change lives on
        # the invitation, not on a user_roles row.
        exists = (
            await session.execute(
                text("SELECT 1 FROM users WHERE id = :uid"), {"uid": target_user_id}
            )
        ).scalar_one_or_none()
        if exists is None:
            raise NotFound("That employee was not found.")
        raise Conflict("That person has not accepted their invitation yet.")

    current_role = Role(current)
    if not outranks(actor_role, current_role):
        raise PermissionDenied("You cannot change the role of someone at or above your rank.")
    if not outranks(actor_role, new_role):
        raise PermissionDenied("You cannot grant a role at or above your own rank.")

    await session.execute(
        text("UPDATE user_roles SET role_key = :role WHERE user_id = :uid"),
        {"role": new_role.value, "uid": target_user_id},
    )
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome, meta_json) "
            "VALUES (:org, :actor, 'user', 'member.role_changed', 'user', :rid, 'success', "
            "cast(:meta AS jsonb))"
        ),
        {
            "org": str(org_id),
            "actor": str(actor_user_id),
            "rid": str(target_user_id),
            "meta": f'{{"from": "{current_role.value}", "to": "{new_role.value}"}}',
        },
    )
    return current_role


# ------------------------------------------------------------------- organisation


async def rename_organisation(
    session: AsyncSession, *, org_id: UUID, actor_user_id: UUID, name: str
) -> str:
    """Rename the organisation. The one org-level setting that is safe to expose.

    The domain is deliberately not editable here: it anchors registration's
    one-org-per-domain rule and the email-verification trust chain, so changing it is an
    identity operation with its own ceremony, not a settings field.
    """
    cleaned = name.strip()
    if not cleaned:
        raise ValidationFailed("The organisation needs a name.")

    updated = (
        await session.execute(
            text("UPDATE orgs SET name = :name WHERE id = :id RETURNING name"),
            {"name": cleaned, "id": org_id},
        )
    ).scalar_one_or_none()
    if updated is None:
        raise NotFound("Organisation not found.")

    await session.execute(
        text(
            # `:org` and `:rid` hold the same value but must be separate parameters:
            # asyncpg deduplicates a repeated named parameter into one $n, and the two
            # columns type it differently (uuid vs varchar) — an AmbiguousParameterError
            # at prepare time.
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', 'org.renamed', 'org', :rid, 'success')"
        ),
        {"org": str(org_id), "actor": str(actor_user_id), "rid": str(org_id)},
    )
    return str(updated)


# ---------------------------------------------------------------------- overview


@dataclass(frozen=True, slots=True)
class OrgOverview:
    documents: int
    sources: int
    jobs_pending: int
    jobs_running: int
    jobs_failed_24h: int
    jobs_dead_letter: int
    invitations_pending: int
    audit_events_24h: int


async def org_overview(session: AsyncSession) -> OrgOverview:
    """The dashboard's figures. Every one is a count over a real table, none is styled.

    One round trip: the dashboard renders on every admin sign-in, and eight sequential
    queries would put eight sequential latencies in front of it.
    """
    row = (
        await session.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM documents WHERE superseded_by IS NULL) AS documents, "
                "(SELECT count(*) FROM sources) AS sources, "
                "(SELECT count(*) FROM jobs WHERE state IN ('pending', 'retry_scheduled')) AS jobs_pending, "
                "(SELECT count(*) FROM jobs WHERE state NOT IN ('pending', 'retry_scheduled', 'completed', 'failed', 'dead_letter')) AS jobs_running, "
                "(SELECT count(*) FROM jobs WHERE state IN ('failed', 'dead_letter') "
                " AND updated_at > now() - interval '24 hours') AS jobs_failed_24h, "
                "(SELECT count(*) FROM jobs WHERE state = 'dead_letter') AS jobs_dead_letter, "
                "(SELECT count(*) FROM invitations WHERE accepted_at IS NULL "
                " AND revoked_at IS NULL AND expires_at > now()) AS invitations_pending, "
                "(SELECT count(*) FROM audit_log WHERE ts > now() - interval '24 hours') "
                " AS audit_events_24h"
            )
        )
    ).one()
    return OrgOverview(
        documents=row.documents,
        sources=row.sources,
        jobs_pending=row.jobs_pending,
        jobs_running=row.jobs_running,
        jobs_failed_24h=row.jobs_failed_24h,
        jobs_dead_letter=row.jobs_dead_letter,
        invitations_pending=row.invitations_pending,
        audit_events_24h=row.audit_events_24h,
    )
