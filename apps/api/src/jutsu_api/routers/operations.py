"""Operational surfaces: the audit trail, the job queue, and knowledge sources.

Three read-only routers' worth of endpoints in one file, because they share a shape:
org-scoped lists over tables the ingestion and identity slices write, paginated by
keyset, gated by the permission the admin console's navigation already names for them.

Permissions differ deliberately:

  GET /v1/audit    audit:read        the trail names people and actions
  GET /v1/jobs     org:read          queue state is organisational telemetry
  GET /v1/sources  integration:read  connector state is the integration surface
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from jutsu_core.rbac import Permission, Role, role_label
from pydantic import BaseModel
from sqlalchemy import text

from jutsu_api.deps import CurrentPrincipal, Db
from jutsu_api.operations import (
    list_audit,
    list_jobs,
    list_sources,
    read_job_stats,
)
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
    system: str
    status: str
    last_sync_at: datetime | None
    document_count: int
    jobs_pending: int
    jobs_completed: int
    jobs_failed: int
    last_walk: dict[str, int]


class SourcePageOut(BaseModel):
    items: list[SourceEntry]


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
) -> AuditPageOut:
    """The organisation's immutable trail, newest first.

    Read-only is not a convention here — migration 0002 revoked UPDATE and DELETE on
    `audit_log` from the application role, so this endpoint could not tamper with the
    trail even if it were wrong.
    """
    page = await list_audit(
        session,
        limit=limit,
        cursor=cursor,
        action=action,
        outcome=outcome,
        resource_type=resource_type,
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
