"""Operational surfaces: the audit trail, the job queue, and knowledge sources.

Three routers' worth of endpoints in one file, because they share a shape: org-scoped
lists over tables the ingestion and identity slices write, paginated by keyset, gated by
the permission the admin console's navigation already names for them.

Permissions differ deliberately:

  GET  /v1/audit               audit:read           the trail names people and actions
  GET  /v1/jobs                org:read             queue state is organisational telemetry
  GET  /v1/sources             integration:read     connector state is the integration surface
  POST /v1/sources/{id}/sync   integration:connect  re-running ingestion is an integration act

The one write here is the last line: reading a source's health and *acting* on it are
separate privileges, so an Analyst keeps the watch and the IT Admin keeps the button.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status
from jutsu_core.rbac import Permission, Role, role_label
from pydantic import BaseModel
from sqlalchemy import text

from jutsu_api.deps import CurrentPrincipal, Db
from jutsu_api.operations import (
    list_audit,
    list_jobs,
    list_sources,
    read_job_stats,
    request_source_sync,
)
from jutsu_api.queue import ring_doorbell
from jutsu_api.security import GuardedAPIRoute, requires

router = APIRouter(prefix="/v1", tags=["operations"], route_class=GuardedAPIRoute)


class AuditEntry(BaseModel):
    id: int
    actor_id: str | None
    actor_jutsu_id: str | None
    actor_type: str
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    ts: datetime
    correlation_id: str | None


class AuditPageOut(BaseModel):
    items: list[AuditEntry]
    next_cursor: str | None


class JobEntry(BaseModel):
    id: UUID
    kind: str
    state: str
    attempts: int
    failure_kind: str | None
    created_at: datetime
    updated_at: datetime


class JobPageOut(BaseModel):
    items: list[JobEntry]
    next_cursor: str | None


class JobStatsOut(BaseModel):
    by_state: dict[str, int]
    dead_letter: int
    failed_24h: int


class SourceEntry(BaseModel):
    id: UUID
    #: The ACL namespace. Four Google products share `gmail` and three Microsoft ones
    #: share `m365`, so this identifies a family, never a source.
    system: str
    #: The provider registry id, or None for a local source. This is what a person
    #: recognises — "Google Drive", not "gmail".
    provider: str | None
    #: Whose account it reads, as the provider labelled it. None for a local source.
    account_label: str | None
    status: str
    last_sync_at: datetime | None
    document_count: int
    jobs_pending: int
    jobs_completed: int
    jobs_failed: int
    last_walk: dict[str, int]


class SourcePageOut(BaseModel):
    items: list[SourceEntry]


class SourceSyncQueued(BaseModel):
    #: The row that will actually run — never a fresh id that names nothing, so the Jobs
    #: page can be asked about it.
    job_id: UUID


@router.get("/audit")
@requires(Permission.AUDIT_READ)
async def read_audit(
    principal: CurrentPrincipal,
    session: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    outcome: Annotated[str | None, Query(max_length=16)] = None,
    resource_type: Annotated[str | None, Query(max_length=64)] = None,
    resource_id: Annotated[str | None, Query(max_length=255)] = None,
) -> AuditPageOut:
    """The organisation's immutable trail, newest first.

    Read-only is not a convention here — migration 0002 revoked UPDATE and DELETE on
    `audit_log` from the application role, so this endpoint could not tamper with the
    trail even if it were wrong.

    `resource_id` is an opaque id (a package id, say), never a name or an address — the
    trail's own columns hold nothing else, so nothing else can be asked of it.
    """
    page = await list_audit(
        session,
        limit=limit,
        cursor=cursor,
        action=action,
        outcome=outcome,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    return AuditPageOut(
        items=[AuditEntry(**asdict(row)) for row in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/jobs")
@requires(Permission.ORG_READ)
async def read_jobs(
    principal: CurrentPrincipal,
    session: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    state: Annotated[str | None, Query(max_length=32)] = None,
    kind: Annotated[str | None, Query(max_length=64)] = None,
) -> JobPageOut:
    """Ingestion and embedding jobs, most recently touched first.

    Carries the classified `failure_kind`, never the exception text: error strings can
    embed file paths and provider payloads, which §4.9 keeps out of anything renderable.
    """
    # Looking at the queue wakes the worker for this organisation. This is the recovery
    # ADR 0012 left open: a tenant whose doorbell was lost is drained the moment
    # somebody comes to see why nothing moved. Org-scoped, coalesced, best-effort.
    await ring_doorbell(principal.org_id)
    page = await list_jobs(session, limit=limit, cursor=cursor, state=state, kind=kind)
    return JobPageOut(
        items=[JobEntry(**asdict(row)) for row in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/jobs/stats")
@requires(Permission.ORG_READ)
async def read_jobs_stats(principal: CurrentPrincipal, session: Db) -> JobStatsOut:
    stats = await read_job_stats(session)
    return JobStatsOut(**asdict(stats))


@router.get("/sources")
@requires(Permission.INTEGRATION_READ)
async def read_sources(principal: CurrentPrincipal, session: Db) -> SourcePageOut:
    """Every knowledge source, with sync state and what it has produced.

    Returns configuration *state*, never configuration *content* — `config_json` holds
    corpus paths and connector settings that describe infrastructure, and no UI needs
    them to render a health row.
    """
    rows = await list_sources(session)
    return SourcePageOut(items=[SourceEntry(**asdict(row)) for row in rows])


@router.post("/sources/{source_id}/sync", status_code=status.HTTP_202_ACCEPTED)
@requires(Permission.INTEGRATION_CONNECT)
async def resync_source(
    source_id: UUID, principal: CurrentPrincipal, session: Db
) -> SourceSyncQueued:
    """Re-run ingestion for one source. 202, because the walk happens elsewhere.

    Gated on `integration:connect` rather than on `integration:read`: an Analyst may
    watch a stalled source, and only the roles that configure connectors may act on one.
    A source belonging to another organisation is a 404 — the service never asks whose
    it is, and RLS answers that question by finding nothing.
    """
    job_id = await request_source_sync(
        session,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        source_id=source_id,
    )
    # Wake the worker for this organisation. Best-effort by contract: the job row is
    # durable, and the message is deferred past this request's commit so the drain
    # cannot arrive before the row it is coming for.
    await ring_doorbell(principal.org_id)
    return SourceSyncQueued(job_id=job_id)


class RoleDescription(BaseModel):
    key: Role
    label: str
    rank: int
    permissions: list[Permission]


class RoleCatalogue(BaseModel):
    roles: list[RoleDescription]


@router.get("/roles")
@requires(Permission.ORG_READ)
async def read_roles(principal: CurrentPrincipal, session: Db) -> RoleCatalogue:
    """The role catalogue: every role, its rank and what it may do.

    Read from the DATABASE, not from `jutsu_core.rbac`, deliberately. The database is the
    runtime authority — migration 0002 seeds it and then revokes writes — and this
    endpoint describing anything else would be the UI documenting the authoring copy
    while enforcement follows the seeded one. `test_rbac_catalogue` asserts they are
    identical, so in practice they agree; the principle is about which one answers.

    The catalogue is org-independent by design, which is why nothing here filters by
    tenant: roles and permissions are product vocabulary, not customer data.
    """
    rows = (
        await session.execute(
            text(
                "SELECT r.key, r.rank, rp.permission_key FROM roles r "
                "LEFT JOIN role_permissions rp ON rp.role_key = r.key "
                "ORDER BY r.rank DESC, r.key, rp.permission_key"
            )
        )
    ).all()

    grouped: dict[str, RoleDescription] = {}
    for row in rows:
        entry = grouped.get(row.key)
        if entry is None:
            role = Role(row.key)
            entry = RoleDescription(key=role, label=role_label(role), rank=row.rank, permissions=[])
            grouped[row.key] = entry
        if row.permission_key is not None:
            entry.permissions.append(Permission(row.permission_key))
    return RoleCatalogue(roles=list(grouped.values()))
