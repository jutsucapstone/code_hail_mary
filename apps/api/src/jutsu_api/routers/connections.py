"""Connections over HTTP: the employee lifecycle and the administrative governance.

Two different permissions guard two different worlds, and no endpoint serves both:

  self  integration:self_manage   the caller's OWN connections — catalogue, connect,
                                  callback, disconnect, sync. No user parameter exists
                                  on any of them; the subject is always the session.
  admin integration:read/revoke   aggregates, one employee's list, revocation, policy.
        org:update                policy writes.

**No response in this file can carry a token.** Credentials live in a table nothing
here selects from; the response models below are the complete serialization surface,
and the test suite greps them to keep it that way.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import RedirectResponse
from jutsu_core.rbac import Permission
from pydantic import BaseModel

from jutsu_api.connectors import (
    HttpOAuthTransport,
    OAuthTransport,
    complete_callback,
    connection_summary,
    disconnect_own,
    employee_connections,
    list_catalogue,
    list_policies,
    revoke_connection,
    set_policy,
    start_connection,
    sync_now,
)
from jutsu_api.deps import CurrentPrincipal, Db, SettingsDep
from jutsu_api.security import GuardedAPIRoute, requires

router = APIRouter(prefix="/v1", tags=["connections"], route_class=GuardedAPIRoute)


def get_oauth_transport() -> OAuthTransport:
    """The provider round trip. Tests override this exactly like the email sender."""
    return HttpOAuthTransport()


TransportDep = Annotated[OAuthTransport, Depends(get_oauth_transport)]


class ConnectionOut(BaseModel):
    id: UUID
    provider: str
    status: str
    account_label: str | None
    connected_at: datetime | None
    last_sync_at: datetime | None
    last_error_kind: str | None


class CatalogueEntryOut(BaseModel):
    id: str
    name: str
    group: str
    group_label: str
    description: str
    configured: bool
    allowed: bool
    connection: ConnectionOut | None


class CatalogueOut(BaseModel):
    items: list[CatalogueEntryOut]


class ConnectStarted(BaseModel):
    connection_id: UUID
    #: Where the browser goes next. The frontend navigates; it never fetches this URL.
    authorize_url: str


class SyncQueued(BaseModel):
    job_id: UUID
    status: str = "queued"


class ProviderSummaryOut(BaseModel):
    provider: str
    name: str
    total: int
    by_status: dict[str, int]


class SummaryOut(BaseModel):
    items: list[ProviderSummaryOut]


class EmployeeConnectionsOut(BaseModel):
    items: list[ConnectionOut]


class PolicyOut(BaseModel):
    provider: str
    name: str
    allowed: bool


class PoliciesOut(BaseModel):
    items: list[PolicyOut]


class PolicyPayload(BaseModel):
    model_config = {"extra": "forbid"}

    allowed: bool


# ----------------------------------------------------------------------- employee


@router.get("/integrations")
@requires(Permission.INTEGRATION_SELF_MANAGE)
async def read_catalogue(principal: CurrentPrincipal, session: Db) -> CatalogueOut:
    """The full catalogue with the caller's own state merged in.

    `configured: false` is a fact about this deployment, `allowed: false` a fact about
    this organisation, and both render as honest refusals — never as a Connect button
    that pretends.
    """
    entries = await list_catalogue(session, user_id=principal.user_id)
    return CatalogueOut(items=[CatalogueEntryOut(**asdict(entry)) for entry in entries])


@router.post("/me/connections/{provider_id}", status_code=status.HTTP_201_CREATED)
@requires(Permission.INTEGRATION_SELF_MANAGE)
async def connect(
    provider_id: str, principal: CurrentPrincipal, session: Db, settings: SettingsDep
) -> ConnectStarted:
    """Begin the OAuth flow for the CALLING employee.

    There is deliberately no variant that names another user: administrators govern
    connections, they do not create them on people's behalf (§2).
    """
    flow = await start_connection(
        session,
        settings,
        org_id=principal.org_id,
        user_id=principal.user_id,
        provider_id=provider_id,
    )
    return ConnectStarted(connection_id=flow.connection_id, authorize_url=flow.authorize_url)


@router.get("/connections/callback", include_in_schema=False)
@requires(Permission.INTEGRATION_SELF_MANAGE)
async def oauth_callback(
    principal: CurrentPrincipal,
    session: Db,
    settings: SettingsDep,
    transport: TransportDep,
    response: Response,
    state: str = Query(min_length=16, max_length=128),
    code: str = Query(min_length=1, max_length=2048),
) -> RedirectResponse:
    """The provider's redirect lands here, through the web proxy, with the session.

    A state-changing GET, exceptionally: OAuth providers redirect with GET and can send
    no CSRF header. The single-use `state` value — minted at connect, matched against
    the caller's own connecting row, spent before the exchange — is the defence, and it
    is the standard one for exactly this flow.

    Out of the OpenAPI schema because no client calls it: browsers arrive here.
    """
    view = await complete_callback(
        session,
        settings,
        transport,
        org_id=principal.org_id,
        user_id=principal.user_id,
        state=state,
        code=code,
    )
    # Back to the page that started it, with nothing sensitive in the URL.
    return RedirectResponse(
        url=f"/me/integrations?connected={view.provider}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.delete("/me/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
@requires(Permission.INTEGRATION_SELF_MANAGE)
async def disconnect(connection_id: UUID, principal: CurrentPrincipal, session: Db) -> None:
    """Disconnect the caller's OWN connection; anyone else's row is a 404 here."""
    await disconnect_own(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        connection_id=connection_id,
    )


@router.post("/me/connections/{connection_id}/sync", status_code=status.HTTP_202_ACCEPTED)
@requires(Permission.INTEGRATION_SELF_MANAGE)
async def request_sync(connection_id: UUID, principal: CurrentPrincipal, session: Db) -> SyncQueued:
    job_id = await sync_now(
        session,
        org_id=principal.org_id,
        user_id=principal.user_id,
        connection_id=connection_id,
    )
    return SyncQueued(job_id=job_id)


# ----------------------------------------------------------------------- governance


@router.get("/connections/summary")
@requires(Permission.INTEGRATION_READ)
async def read_summary(principal: CurrentPrincipal, session: Db) -> SummaryOut:
    """Counts per provider per status. Governance reads numbers, not identities."""
    items = await connection_summary(session)
    return SummaryOut(items=[ProviderSummaryOut(**asdict(item)) for item in items])


@router.get("/employees/{user_id}/connections")
@requires(Permission.INTEGRATION_READ)
async def read_employee_connections(
    user_id: UUID, principal: CurrentPrincipal, session: Db
) -> EmployeeConnectionsOut:
    rows = await employee_connections(session, user_id=user_id)
    return EmployeeConnectionsOut(items=[ConnectionOut(**asdict(row)) for row in rows])


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
@requires(Permission.INTEGRATION_REVOKE)
async def revoke(connection_id: UUID, principal: CurrentPrincipal, session: Db) -> None:
    """Administrative revocation: rank-checked in the service, audited per connection."""
    await revoke_connection(
        session,
        org_id=principal.org_id,
        actor_id=principal.user_id,
        actor_role=principal.role,
        connection_id=connection_id,
    )


@router.get("/connection-policies")
@requires(Permission.INTEGRATION_READ)
async def read_policies(principal: CurrentPrincipal, session: Db) -> PoliciesOut:
    items = await list_policies(session)
    return PoliciesOut(items=[PolicyOut(**asdict(item)) for item in items])


@router.put("/connection-policies/{provider_id}")
@requires(Permission.ORG_UPDATE)
async def write_policy(
    provider_id: str, payload: PolicyPayload, principal: CurrentPrincipal, session: Db
) -> PolicyOut:
    """Allow or restrict one provider organisation-wide.

    Restricting does not sever existing connections — a policy toggle must not be a
    silent mass revocation. The summary keeps showing them; revoking is per-connection
    and per-person, where the audit trail can name what happened.
    """
    row = await set_policy(
        session,
        org_id=principal.org_id,
        actor_id=principal.user_id,
        provider_id=provider_id,
        allowed=payload.allowed,
    )
    return PolicyOut(**asdict(row))
