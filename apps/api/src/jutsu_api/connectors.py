"""Employee-owned connections: the provider registry, the OAuth flow, and governance.

The product principle (§2): employees connect and authorize their own applications;
administrators govern, monitor and audit those connections. Every function here keeps
one of the promises that sentence makes:

* `start_connection` / `complete_callback` run only for the session that owns the row —
  an administrator has no way to connect on someone's behalf, because the endpoint that
  starts a flow takes no user parameter at all.
* `disconnect_own` checks row ownership explicitly. Row-level security scopes the
  *organisation*; it says nothing about which member inside it, and without the check
  any colleague could sever anyone's connection.
* `revoke_connection` is the administrative act, and it is rank-checked and audited the
  same way identity revocation is.

**Tokens never leave this module unencrypted, and never leave the service at all.** The
Fernet key comes from Secret Manager (`JUTSU_CONNECTION_KEY`); ciphertext goes to
`connection_credentials`, a table no response model reads. Nothing in this file returns
a token, and the tests pin that with a search over serialized responses.

**Honesty about configuration.** A provider with no client credentials in the
environment is reported `configured: false` and refuses to start a flow with a 503 —
the UI then says "not configured for this deployment" instead of pretending. No fake
OAuth, ever: the flow either round-trips a real provider or does not begin.

**A connection grants no document visibility.** The OAuth callback proves a provider
subject the way email verification proves an address, and the subject is stored on the
row — but mapping thirteen providers onto the seven ACL namespaces is an authorization
decision that needs its own ADR before anything writes `source_identities` from here.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet
from jutsu_core.errors import Conflict, NotFound, PermissionDenied, ServiceUnavailable
from jutsu_core.rbac import Role, outranks
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.config import Settings

__all__ = [
    "PROVIDERS",
    "AccountIdentity",
    "CatalogueEntry",
    "ConnectionView",
    "HttpOAuthTransport",
    "OAuthTransport",
    "Provider",
    "TokenGrant",
    "complete_callback",
    "connection_summary",
    "disconnect_own",
    "employee_connections",
    "list_catalogue",
    "list_policies",
    "oauth_client_for",
    "revoke_connection",
    "set_policy",
    "start_connection",
    "sync_now",
]


# ------------------------------------------------------------------------ registry


@dataclass(frozen=True, slots=True)
class Provider:
    id: str
    name: str
    #: Grouping for the catalogue UI: google | microsoft | communication | engineering.
    group: str
    description: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    #: Read-only scopes, and nothing else, ever (§4.8). A write scope in this tuple is a
    #: defect whatever feature wanted it.
    scopes: tuple[str, ...]


_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, not a secret
_GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
_MS_AUTH = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_MS_TOKEN = "https://login.microsoftonline.com/common/oauth2/v2.0/token"  # noqa: S105
_MS_USERINFO = "https://graph.microsoft.com/v1.0/me"


def _google(id_: str, name: str, description: str, *scopes: str) -> Provider:
    return Provider(
        id=id_,
        name=name,
        group="google",
        description=description,
        authorize_url=_GOOGLE_AUTH,
        token_url=_GOOGLE_TOKEN,
        userinfo_url=_GOOGLE_USERINFO,
        scopes=("openid", "email", *scopes),
    )


def _microsoft(id_: str, name: str, description: str, *scopes: str) -> Provider:
    return Provider(
        id=id_,
        name=name,
        group="microsoft",
        description=description,
        authorize_url=_MS_AUTH,
        token_url=_MS_TOKEN,
        userinfo_url=_MS_USERINFO,
        scopes=("openid", "email", "offline_access", *scopes),
    )


#: The catalogue. Mirrors migration 0012's CHECK constraint — a test inserts every id
#: to prove the two lists cannot drift apart silently.
PROVIDERS: dict[str, Provider] = {
    p.id: p
    for p in (
        _google(
            "google_drive",
            "Google Drive",
            "Documents and files you can already open in Drive.",
            "https://www.googleapis.com/auth/drive.readonly",
        ),
        _google(
            "gmail",
            "Gmail",
            "Mail in your mailbox, evaluated against policy before anything is kept.",
            "https://www.googleapis.com/auth/gmail.readonly",
        ),
        _google(
            "google_calendar",
            "Google Calendar",
            "Meetings and attendees from your calendar.",
            "https://www.googleapis.com/auth/calendar.readonly",
        ),
        _google(
            "google_meet",
            "Google Meet",
            "Meeting records and artefacts you have access to.",
            "https://www.googleapis.com/auth/meetings.space.readonly",
        ),
        _microsoft(
            "onedrive",
            "OneDrive",
            "Files you can already open in OneDrive.",
            "Files.Read",
        ),
        _microsoft(
            "teams",
            "Microsoft Teams",
            "Channels and chats you belong to.",
            "Chat.Read",
            "ChannelMessage.Read.All",
        ),
        _microsoft(
            "sharepoint",
            "SharePoint",
            "Sites and documents you can already reach.",
            "Sites.Read.All",
        ),
        Provider(
            id="slack",
            name="Slack",
            group="communication",
            description="Conversations in channels you are a member of.",
            authorize_url="https://slack.com/oauth/v2/authorize",
            token_url="https://slack.com/api/oauth.v2.access",  # noqa: S106
            userinfo_url="https://slack.com/api/users.identity",
            scopes=("channels:history", "channels:read", "users:read"),
        ),
        Provider(
            id="jira",
            name="Jira",
            group="engineering",
            description="Issues and projects you can already see.",
            authorize_url="https://auth.atlassian.com/authorize",
            token_url="https://auth.atlassian.com/oauth/token",  # noqa: S106
            userinfo_url="https://api.atlassian.com/me",
            scopes=("read:jira-work", "read:jira-user", "offline_access"),
        ),
        Provider(
            id="confluence",
            name="Confluence",
            group="engineering",
            description="Pages and spaces you can already read.",
            authorize_url="https://auth.atlassian.com/authorize",
            token_url="https://auth.atlassian.com/oauth/token",  # noqa: S106
            userinfo_url="https://api.atlassian.com/me",
            scopes=("read:confluence-content.all", "read:confluence-user", "offline_access"),
        ),
        Provider(
            id="github",
            name="GitHub",
            group="engineering",
            description="Repositories, issues and pull requests you can already see.",
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",  # noqa: S106
            userinfo_url="https://api.github.com/user",
            scopes=("read:user", "repo:status", "read:org"),
        ),
    )
}

GROUP_LABELS = {
    "google": "Google Workspace",
    "microsoft": "Microsoft",
    "communication": "Communication",
    "engineering": "Engineering",
}


@dataclass(frozen=True, slots=True)
class OAuthClient:
    client_id: str
    client_secret: str


def oauth_client_for(provider_id: str) -> OAuthClient | None:
    """The deployment's client registration for one provider, or None.

    Read from the environment at call time rather than frozen into Settings: these are
    per-deployment operational secrets (Secret Manager in production, `.env` locally),
    and their absence is a meaningful state the catalogue reports rather than an error.
    """
    prefix = f"JUTSU_OAUTH_{provider_id.upper()}"
    client_id = os.environ.get(f"{prefix}_CLIENT_ID", "").strip()
    client_secret = os.environ.get(f"{prefix}_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        return OAuthClient(client_id=client_id, client_secret=client_secret)
    return None


def _fernet() -> Fernet | None:
    key = os.environ.get("JUTSU_CONNECTION_KEY", "").strip()
    if not key:
        return None
    return Fernet(key.encode("ascii"))


def provider_configured(provider_id: str) -> bool:
    """Whether a real flow can run: client credentials AND an encryption key.

    Both, deliberately: credentials without a key would complete an exchange and then
    have nowhere lawful to put the token, which is worse than refusing to start.
    """
    return oauth_client_for(provider_id) is not None and _fernet() is not None


# ------------------------------------------------------------------------ transport


@dataclass(frozen=True, slots=True)
class TokenGrant:
    access_token: str
    refresh_token: str | None
    expires_in: int | None


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    #: The provider's stable subject for this account. Proven, stored, and — see the
    #: module docstring — granting nothing until the namespace-mapping ADR exists.
    subject: str
    #: How the account shows itself to its owner: an address or account name.
    label: str


class OAuthTransport(Protocol):
    """The two provider calls the callback makes, behind a seam.

    A Protocol for the same reason the embedding client has one: the real implementation
    talks to the outside world, and every test that needs a completed flow injects a
    fake rather than the suite billing a provider — or worse, faking success silently.
    """

    async def exchange_code(
        self, provider: Provider, client: OAuthClient, *, code: str, redirect_uri: str
    ) -> TokenGrant: ...

    async def fetch_identity(self, provider: Provider, access_token: str) -> AccountIdentity: ...


class HttpOAuthTransport:
    """The real exchange, over HTTPS, with the provider named by the registry."""

    async def exchange_code(
        self, provider: Provider, client: OAuthClient, *, code: str, redirect_uri: str
    ) -> TokenGrant:
        async with httpx.AsyncClient(timeout=20.0) as http:
            response = await http.post(
                provider.token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client.client_id,
                    "client_secret": client.client_secret,
                },
                headers={"Accept": "application/json"},
            )
        if response.status_code != 200:
            # The body can carry the code and client id back; classify, never forward.
            raise ServiceUnavailable("The provider refused the token exchange.")
        payload = response.json()
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ServiceUnavailable("The provider's token response was not usable.")
        refresh = payload.get("refresh_token")
        expires = payload.get("expires_in")
        return TokenGrant(
            access_token=access_token,
            refresh_token=refresh if isinstance(refresh, str) else None,
            expires_in=int(expires) if isinstance(expires, int) else None,
        )

    async def fetch_identity(self, provider: Provider, access_token: str) -> AccountIdentity:
        async with httpx.AsyncClient(timeout=20.0) as http:
            response = await http.get(
                provider.userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code != 200:
            raise ServiceUnavailable("The provider did not answer for the account identity.")
        payload = response.json()
        subject = str(
            payload.get("sub") or payload.get("account_id") or payload.get("id") or ""
        ).strip()
        label = str(
            payload.get("email") or payload.get("login") or payload.get("name") or subject
        ).strip()
        if not subject:
            raise ServiceUnavailable("The provider's identity response carried no subject.")
        return AccountIdentity(subject=subject, label=label)


# ------------------------------------------------------------------------ views


@dataclass(frozen=True, slots=True)
class ConnectionView:
    id: UUID
    provider: str
    status: str
    account_label: str | None
    connected_at: datetime | None
    last_sync_at: datetime | None
    last_error_kind: str | None


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    id: str
    name: str
    group: str
    group_label: str
    description: str
    #: Whether this deployment holds client credentials and an encryption key. False
    #: renders as "not configured for this deployment" — never as a fake Connect.
    configured: bool
    #: The organisation's policy. Absence of a row means allowed.
    allowed: bool
    #: The caller's own live connection, if any.
    connection: ConnectionView | None


def _view(row: object) -> ConnectionView:
    return ConnectionView(
        id=row.id,  # type: ignore[attr-defined]
        provider=row.provider,  # type: ignore[attr-defined]
        status=row.status,  # type: ignore[attr-defined]
        account_label=row.account_label,  # type: ignore[attr-defined]
        connected_at=row.connected_at,  # type: ignore[attr-defined]
        last_sync_at=row.last_sync_at,  # type: ignore[attr-defined]
        last_error_kind=row.last_error_kind,  # type: ignore[attr-defined]
    )


_CONNECTION_COLUMNS = (
    "id, provider, status, account_label, connected_at, last_sync_at, last_error_kind"
)


async def _policy_map(session: AsyncSession) -> dict[str, bool]:
    rows = (await session.execute(text("SELECT provider, allowed FROM connection_policies"))).all()
    return {r.provider: r.allowed for r in rows}


async def list_catalogue(session: AsyncSession, *, user_id: UUID) -> list[CatalogueEntry]:
    """Everything the My Integrations page needs, in one query round trip per table.

    The catalogue itself is registry data; what the database contributes is the caller's
    own live connections and the organisation's policy rows.
    """
    own = (
        await session.execute(
            text(
                f"SELECT {_CONNECTION_COLUMNS} FROM connections "  # noqa: S608
                "WHERE user_id = :user AND status != 'disconnected'"
            ),
            {"user": user_id},
        )
    ).all()
    by_provider = {r.provider: _view(r) for r in own}
    policies = await _policy_map(session)

    return [
        CatalogueEntry(
            id=provider.id,
            name=provider.name,
            group=provider.group,
            group_label=GROUP_LABELS[provider.group],
            description=provider.description,
            configured=provider_configured(provider.id),
            allowed=policies.get(provider.id, True),
            connection=by_provider.get(provider.id),
        )
        for provider in PROVIDERS.values()
    ]


# ------------------------------------------------------------------------ lifecycle


async def _audit(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    action: str,
    resource_id: UUID | str,
    outcome: str = "success",
) -> None:
    await session.execute(
        text(
            "INSERT INTO audit_log (org_id, actor_id, actor_type, action, resource_type, "
            "resource_id, outcome) "
            "VALUES (:org, :actor, 'user', :action, 'connection', :rid, :outcome)"
        ),
        {
            "org": str(org_id),
            "actor": str(actor_id),
            "action": action,
            "rid": str(resource_id),
            "outcome": outcome,
        },
    )


@dataclass(frozen=True, slots=True)
class StartedFlow:
    connection_id: UUID
    authorize_url: str


def _redirect_uri(settings: Settings) -> str:
    """The callback, on the WEB origin, through the proxy.

    Through `app_url` deliberately: the provider redirects the person's browser, and the
    session cookie is first-party only on the console's origin. Pointing the redirect at
    the API origin directly would arrive without a session and be indistinguishable from
    an attack.
    """
    return f"{settings.app_url}/api/jutsu/v1/connections/callback"


async def start_connection(
    session: AsyncSession,
    settings: Settings,
    *,
    org_id: UUID,
    user_id: UUID,
    provider_id: str,
) -> StartedFlow:
    """Begin the OAuth round trip for the calling employee.

    Order of refusals matters: an unknown provider is a 404 about the request, policy is
    the organisation's decision (403), configuration is the deployment's state (503).
    Each message says which, because "cannot connect" without a reason generates a
    support ticket that says exactly that.
    """
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise NotFound("No such integration.")

    policies = await _policy_map(session)
    if not policies.get(provider_id, True):
        raise PermissionDenied("Your organisation does not allow connecting this application.")

    client = oauth_client_for(provider_id)
    if client is None or _fernet() is None:
        raise ServiceUnavailable(
            f"{provider.name} is not configured for this deployment yet. An administrator "
            "must add its client credentials before anyone can connect."
        )

    # One live connection per provider per person — the partial unique index enforces
    # it, but checking first turns a constraint violation into a sentence.
    existing = (
        await session.execute(
            text(
                "SELECT id, status FROM connections "
                "WHERE user_id = :user AND provider = :provider AND status != 'disconnected'"
            ),
            {"user": user_id, "provider": provider_id},
        )
    ).first()
    if existing is not None and existing.status not in ("connecting", "error", "reauth_required"):
        raise Conflict(f"{provider.name} is already connected.")

    state = secrets.token_urlsafe(32)
    if existing is not None:
        connection_id = existing.id
        await session.execute(
            text(
                "UPDATE connections SET oauth_state = :state, status = 'connecting', "
                "updated_at = now() WHERE id = :id"
            ),
            {"state": state, "id": connection_id},
        )
    else:
        connection_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO connections (id, org_id, user_id, provider, status, oauth_state, "
                "scopes) VALUES (:id, :org, :user, :provider, 'connecting', :state, "
                "cast(:scopes AS jsonb))"
            ),
            {
                "id": connection_id,
                "org": str(org_id),
                "user": user_id,
                "provider": provider_id,
                "state": state,
                "scopes": '["' + '", "'.join(provider.scopes) + '"]',
            },
        )

    query = urlencode(
        {
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": _redirect_uri(settings),
            "scope": " ".join(provider.scopes),
            "state": state,
            "access_type": "offline",
        }
    )
    return StartedFlow(
        connection_id=connection_id, authorize_url=f"{provider.authorize_url}?{query}"
    )


async def complete_callback(
    session: AsyncSession,
    settings: Settings,
    transport: OAuthTransport,
    *,
    org_id: UUID,
    user_id: UUID,
    state: str,
    code: str,
) -> ConnectionView:
    """Finish the round trip: state must match a connecting row owned by THIS caller.

    The state parameter is the CSRF defence for the one state-changing GET in the
    product (the provider redirects with GET; there is no header to demand). It is
    single-use — cleared before the exchange — so a replayed callback finds nothing.
    """
    row = (
        await session.execute(
            text(
                "SELECT id, provider, user_id FROM connections "
                "WHERE oauth_state = :state AND status = 'connecting'"
            ),
            {"state": state},
        )
    ).first()
    if row is None or row.user_id != user_id:
        # Not the owner, or a stale/forged state: identical answer, nothing to probe.
        raise NotFound("That connection attempt was not found. Start again from Integrations.")

    provider = PROVIDERS[row.provider]
    client = oauth_client_for(row.provider)
    fernet = _fernet()
    if client is None or fernet is None:
        raise ServiceUnavailable("This integration is no longer configured.")

    # Spend the state before the exchange: a second callback with the same state must
    # find nothing, whatever happens next.
    await session.execute(
        text("UPDATE connections SET oauth_state = NULL, updated_at = now() WHERE id = :id"),
        {"id": row.id},
    )

    grant = await transport.exchange_code(
        provider, client, code=code, redirect_uri=_redirect_uri(settings)
    )
    identity = await transport.fetch_identity(provider, grant.access_token)

    await session.execute(
        text(
            "INSERT INTO connection_credentials (connection_id, org_id, access_token_enc, "
            "refresh_token_enc, token_expires_at) "
            "VALUES (:id, :org, :access, :refresh, :expires_at) "
            "ON CONFLICT (connection_id) DO UPDATE SET "
            "access_token_enc = EXCLUDED.access_token_enc, "
            "refresh_token_enc = EXCLUDED.refresh_token_enc, "
            "token_expires_at = EXCLUDED.token_expires_at, updated_at = now()"
        ),
        {
            "id": row.id,
            "org": str(org_id),
            "access": fernet.encrypt(grant.access_token.encode("utf-8")),
            "refresh": (
                fernet.encrypt(grant.refresh_token.encode("utf-8")) if grant.refresh_token else None
            ),
            # Computed here rather than in SQL: a doubly-referenced parameter inside a
            # CASE cannot be typed by asyncpg (AmbiguousParameterError on $5).
            "expires_at": (
                datetime.now(tz=UTC) + timedelta(seconds=grant.expires_in)
                if grant.expires_in is not None
                else None
            ),
        },
    )
    updated = (
        await session.execute(
            text(
                "UPDATE connections SET status = 'connected', account_label = :label, "  # noqa: S608
                "provider_subject = :subject, connected_at = now(), updated_at = now(), "
                "last_error_kind = NULL WHERE id = :id "
                f"RETURNING {_CONNECTION_COLUMNS}"
            ),
            {"label": identity.label, "subject": identity.subject, "id": row.id},
        )
    ).one()

    await _audit(
        session, org_id=org_id, actor_id=user_id, action="connection.connected", resource_id=row.id
    )
    return _view(updated)


async def disconnect_own(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, connection_id: UUID
) -> None:
    """Disconnect the caller's own connection.

    The ownership predicate is in the UPDATE itself, not a prior SELECT: RLS scopes the
    organisation, and inside one organisation this WHERE clause is the only thing that
    stops a member severing a colleague's connection. A row that is not yours updates
    nothing, and updating nothing is a 404.
    """
    updated = (
        await session.execute(
            text(
                "UPDATE connections SET status = 'disconnected', disconnected_at = now(), "
                "updated_at = now(), oauth_state = NULL "
                "WHERE id = :id AND user_id = :user AND status != 'disconnected' RETURNING id"
            ),
            {"id": connection_id, "user": user_id},
        )
    ).scalar_one_or_none()
    if updated is None:
        raise NotFound("That connection was not found.")

    await session.execute(
        text("DELETE FROM connection_credentials WHERE connection_id = :id"),
        {"id": connection_id},
    )
    await _audit(
        session,
        org_id=org_id,
        actor_id=user_id,
        action="connection.disconnected",
        resource_id=connection_id,
    )


async def revoke_connection(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    actor_role: Role,
    connection_id: UUID,
) -> None:
    """The administrative act: revoke someone's connection, rank-checked and audited.

    An admin may always revoke their own (self-revocation is not an escalation, exactly
    as with identities); anyone else's requires strictly outranking them.
    """
    row = (
        await session.execute(
            text(
                "SELECT c.user_id, ur.role_key FROM connections c "
                "LEFT JOIN user_roles ur ON ur.user_id = c.user_id "
                "WHERE c.id = :id AND c.status != 'disconnected'"
            ),
            {"id": connection_id},
        )
    ).first()
    if row is None:
        raise NotFound("That connection was not found.")

    if row.user_id != actor_id:
        target_role = Role(row.role_key) if row.role_key else Role.MEMBER
        if not outranks(actor_role, target_role):
            raise PermissionDenied(
                "You cannot revoke a connection belonging to someone at or above your rank."
            )

    await session.execute(
        text(
            "UPDATE connections SET status = 'disconnected', disconnected_at = now(), "
            "updated_at = now(), oauth_state = NULL WHERE id = :id"
        ),
        {"id": connection_id},
    )
    await session.execute(
        text("DELETE FROM connection_credentials WHERE connection_id = :id"),
        {"id": connection_id},
    )
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action="connection.revoked",
        resource_id=connection_id,
    )


async def sync_now(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, connection_id: UUID
) -> UUID:
    """Queue a sync for the caller's own connected integration.

    Enqueues a real row in the same durable queue ingestion uses. The worker grows a
    handler for `connector.sync` when the first provider fetcher lands; until then the
    job waits honestly in `pending` where the Jobs page can see it — it is never
    reported as synced.
    """
    row = (
        await session.execute(
            text(
                "SELECT id, provider, status FROM connections "
                "WHERE id = :id AND user_id = :user AND status IN ('connected', 'error')"
            ),
            {"id": connection_id, "user": user_id},
        )
    ).first()
    if row is None:
        raise NotFound("That connection was not found.")
    if not provider_configured(row.provider):
        raise ServiceUnavailable("Syncing is not available until this provider is configured.")

    job_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO jobs (id, org_id, kind, state, idempotency_key, payload_json) "
            "VALUES (:id, :org, 'connector.sync', 'pending', :key, cast(:payload AS jsonb)) "
            "ON CONFLICT (idempotency_key) DO NOTHING"
        ),
        {
            "id": job_id,
            "org": str(org_id),
            "key": f"connector.sync:{connection_id}",
            "payload": f'{{"connection_id": "{connection_id}"}}',
        },
    )
    await _audit(
        session,
        org_id=org_id,
        actor_id=user_id,
        action="connection.sync_requested",
        resource_id=connection_id,
    )
    return job_id


# ------------------------------------------------------------------------ governance


@dataclass(frozen=True, slots=True)
class ProviderSummary:
    provider: str
    name: str
    total: int
    by_status: dict[str, int]


async def connection_summary(session: AsyncSession) -> list[ProviderSummary]:
    """The admin aggregate: counts, never identities.

    "47 employees connected, 44 healthy, 1 requires re-authentication" — governance
    reads numbers, and reading more than numbers here would put every employee's account
    label one click from anyone holding integration:read for no operational reason.
    """
    rows = (
        await session.execute(
            text(
                "SELECT provider, status, count(*) AS n FROM connections "
                "WHERE status != 'disconnected' GROUP BY provider, status"
            )
        )
    ).all()
    grouped: dict[str, dict[str, int]] = {}
    for row in rows:
        grouped.setdefault(row.provider, {})[row.status] = row.n
    return [
        ProviderSummary(
            provider=provider_id,
            name=PROVIDERS[provider_id].name if provider_id in PROVIDERS else provider_id,
            total=sum(statuses.values()),
            by_status=statuses,
        )
        for provider_id, statuses in sorted(grouped.items())
    ]


async def employee_connections(session: AsyncSession, *, user_id: UUID) -> list[ConnectionView]:
    """One employee's connections, for the governance detail view."""
    rows = (
        await session.execute(
            text(
                f"SELECT {_CONNECTION_COLUMNS} FROM connections "  # noqa: S608
                "WHERE user_id = :user AND status != 'disconnected' ORDER BY provider"
            ),
            {"user": user_id},
        )
    ).all()
    return [_view(r) for r in rows]


@dataclass(frozen=True, slots=True)
class PolicyRow:
    provider: str
    name: str
    allowed: bool


async def list_policies(session: AsyncSession) -> list[PolicyRow]:
    stored = await _policy_map(session)
    return [PolicyRow(id_, PROVIDERS[id_].name, stored.get(id_, True)) for id_ in PROVIDERS]


async def set_policy(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, provider_id: str, allowed: bool
) -> PolicyRow:
    """Allow or restrict one provider for the whole organisation.

    Restricting does NOT sever existing connections — that would make a policy toggle a
    mass data-plane action with no confirmation. Existing connections stay visible in
    the summary; revoking them is the explicit per-connection act, audited per person.
    """
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise NotFound("No such integration.")

    await session.execute(
        text(
            "INSERT INTO connection_policies (org_id, provider, allowed, updated_by) "
            "VALUES (:org, :provider, :allowed, :actor) "
            "ON CONFLICT (org_id, provider) DO UPDATE SET allowed = EXCLUDED.allowed, "
            "updated_by = EXCLUDED.updated_by, updated_at = now()"
        ),
        {"org": str(org_id), "provider": provider_id, "allowed": allowed, "actor": actor_id},
    )
    await _audit(
        session,
        org_id=org_id,
        actor_id=actor_id,
        action="connection.policy_changed",
        resource_id=provider_id,
    )
    return PolicyRow(provider_id, provider.name, allowed)
