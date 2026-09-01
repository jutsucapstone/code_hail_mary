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

**Connecting links the proven subject (ADR 0014).** The OAuth callback proves a
provider subject the way email verification proves an address, and the callback links
it as a source identity in the provider's ACL namespace — fail-closed when the subject
is already someone else's. Disconnecting stops the syncing; it deliberately does not
revoke the identity, because who somebody is does not change when a pipe closes.
Identity revocation stays the administrative act it always was.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet, InvalidToken
from jutsu_core.errors import Conflict, NotFound, PermissionDenied, ServiceUnavailable
from jutsu_core.models import SourceSystem
from jutsu_core.providers import (
    GROUP_LABELS,
    PROVIDERS,
    OAuthClient,
    Provider,
    oauth_client_for,
)
from jutsu_core.rbac import Role, outranks
from jutsu_db.engine import org_session
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.config import Settings
from jutsu_api.identities import link_verified_subject

__all__ = [
    "GROUP_LABELS",
    "PROVIDERS",
    "AccountIdentity",
    "CatalogueEntry",
    "ConnectionView",
    "HttpOAuthTransport",
    "OAuthClient",
    "OAuthTransport",
    "Provider",
    "TokenGrant",
    "abandon_callback",
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
# The registry itself lives in jutsu_core.providers — the worker refreshes tokens and
# fetches content against the same catalogue, and two copies of a token URL is how the
# two halves drift. Re-exported here because this module is the API's face for it.

#: How long a minted state (and its PKCE verifier) stays spendable. Long enough for a
#: person to read a consent screen twice; short enough that an abandoned attempt is not
#: a standing invitation.
STATE_TTL = timedelta(minutes=15)


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
        self,
        provider: Provider,
        client: OAuthClient,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> TokenGrant: ...

    async def fetch_identity(self, provider: Provider, access_token: str) -> AccountIdentity: ...

    async def revoke_upstream(
        self, provider: Provider, client: OAuthClient, *, access_token: str
    ) -> None: ...


class HttpOAuthTransport:
    """The real exchange, over HTTPS, with the provider named by the registry.

    An injected `httpx.AsyncClient` is the test seam, the same shape as
    `VertexTransport`: hand one built on `httpx.MockTransport` and every branch below
    runs deterministically without a provider. Absent, each call opens and closes its
    own short-lived client — this transport is constructed per request by a dependency
    with no shutdown hook, so a long-lived owned client would have nowhere to close.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    @asynccontextmanager
    async def _http(self, owned_timeout: float) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
        else:
            async with httpx.AsyncClient(timeout=owned_timeout) as owned:
                yield owned

    async def exchange_code(
        self,
        provider: Provider,
        client: OAuthClient,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> TokenGrant:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
        }
        if code_verifier is not None:
            data["code_verifier"] = code_verifier
        async with self._http(20.0) as http:
            response = await http.post(
                provider.token_url, data=data, headers={"Accept": "application/json"}
            )
        if response.status_code != 200:
            # The body can carry the code and client id back; classify, never forward.
            raise ServiceUnavailable("The provider refused the token exchange.")
        payload = response.json()
        if payload.get("ok") is False:
            # Slack answers HTTP 200 with ok=false; treating that as success would
            # store an error string where a token belongs.
            raise ServiceUnavailable("The provider refused the token exchange.")
        if provider.token_style == "slack_user":  # noqa: S105 - a style tag, not a secret
            # The employee's own token lives in authed_user; a top-level access_token
            # here would be a bot's, which this product never provisions.
            payload = payload.get("authed_user") or {}
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
        async with self._http(20.0) as http:
            response = await http.get(
                provider.userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code != 200:
            raise ServiceUnavailable("The provider did not answer for the account identity.")
        payload = response.json()
        if payload.get("ok") is False:
            raise ServiceUnavailable("The provider did not answer for the account identity.")
        subject = str(
            payload.get("sub")
            or payload.get("account_id")
            or payload.get("user_id")  # Slack auth.test
            or payload.get("id")
            or ""
        ).strip()
        label = str(
            payload.get("email")
            or payload.get("login")
            or payload.get("name")
            or payload.get("user")  # Slack auth.test
            or subject
        ).strip()
        if not subject:
            raise ServiceUnavailable("The provider's identity response carried no subject.")
        return AccountIdentity(subject=subject, label=label)

    async def revoke_upstream(
        self, provider: Provider, client: OAuthClient, *, access_token: str
    ) -> None:
        """Best-effort revocation at the provider on disconnect.

        Deleting the local ciphertext is the authority; this additionally asks the
        provider to kill the grant so the token is dead everywhere, where the provider
        offers an endpoint for it (Google, Slack, GitHub do; Microsoft and Atlassian
        rely on expiry). Failures are swallowed by contract — an unreachable revocation
        endpoint must not stop a person from disconnecting.
        """
        if provider.revoke_url is None or provider.revoke_style is None:
            return
        try:
            async with self._http(10.0) as http:
                if provider.revoke_style == "post_token":
                    await http.post(provider.revoke_url, data={"token": access_token})
                elif provider.revoke_style == "bearer_post":
                    await http.post(
                        provider.revoke_url,
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                elif provider.revoke_style == "github_grant":
                    await http.request(
                        "DELETE",
                        provider.revoke_url.format(client_id=client.client_id),
                        auth=(client.client_id, client.client_secret),
                        json={"access_token": access_token},
                    )
        except httpx.HTTPError:
            return


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
    #: The scopes this grant was made with — shown so the owner can see exactly what
    #: they authorised. Read-only by registry construction; never a token.
    scopes: list[str]
    #: Current documents ingested through this connection's source, measured, never
    #: estimated (§2's "indexed item counts where available").
    document_count: int


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
    scopes = getattr(row, "scopes", None)
    return ConnectionView(
        id=row.id,  # type: ignore[attr-defined]
        provider=row.provider,  # type: ignore[attr-defined]
        status=row.status,  # type: ignore[attr-defined]
        account_label=row.account_label,  # type: ignore[attr-defined]
        connected_at=row.connected_at,  # type: ignore[attr-defined]
        last_sync_at=row.last_sync_at,  # type: ignore[attr-defined]
        last_error_kind=row.last_error_kind,  # type: ignore[attr-defined]
        scopes=list(scopes) if isinstance(scopes, list) else [],
        document_count=int(getattr(row, "document_count", 0) or 0),
    )


_CONNECTION_COLUMNS = (
    "id, provider, status, account_label, connected_at, last_sync_at, last_error_kind"
)

#: The catalogue's richer projection: the grant's scopes and a measured count of the
#: current documents its source has produced. The count subquery drives from sources
#: (one row per connection) and stays out of every mutation path's RETURNING.
_CONNECTION_COLUMNS_WITH_COUNT = (
    "c.id, c.provider, c.status, c.account_label, c.connected_at, c.last_sync_at, "
    "c.last_error_kind, c.scopes, "
    "COALESCE((SELECT count(*) FROM documents d JOIN sources s ON s.id = d.source_id "
    "WHERE s.config_json->>'connection_id' = c.id::text "
    "AND d.superseded_by IS NULL), 0) AS document_count"
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
                f"SELECT {_CONNECTION_COLUMNS_WITH_COUNT} FROM connections c "  # noqa: S608
                "WHERE c.user_id = :user AND c.status != 'disconnected'"
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
    # PKCE (S256). The verifier never leaves the server; the provider sees only its
    # digest, so an intercepted authorization code cannot be exchanged without a value
    # that exists in exactly one row of this database.
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    expires_at = datetime.now(tz=UTC) + STATE_TTL
    if existing is not None:
        connection_id = existing.id
        await session.execute(
            text(
                "UPDATE connections SET oauth_state = :state, status = 'connecting', "
                "oauth_code_verifier = :verifier, oauth_state_expires_at = :expires, "
                "updated_at = now() WHERE id = :id"
            ),
            {
                "state": state,
                "verifier": code_verifier,
                "expires": expires_at,
                "id": connection_id,
            },
        )
    else:
        connection_id = uuid4()
        try:
            await session.execute(
                text(
                    "INSERT INTO connections (id, org_id, user_id, provider, status, oauth_state, "
                    "oauth_code_verifier, oauth_state_expires_at, scopes) "
                    "VALUES (:id, :org, :user, :provider, 'connecting', :state, :verifier, "
                    ":expires, cast(:scopes AS jsonb))"
                ),
                {
                    "id": connection_id,
                    "org": str(org_id),
                    "user": user_id,
                    "provider": provider_id,
                    "state": state,
                    "verifier": code_verifier,
                    "expires": expires_at,
                    "scopes": '["' + '", "'.join(provider.scopes) + '"]',
                },
            )
        except IntegrityError as error:
            # Two tabs racing the same Connect click: both pre-checks saw no row, and
            # uq_connections_live refused the loser. The same sentence as the pre-check,
            # because from the caller's side it is the same fact.
            if "uq_connections_live" not in str(error.orig):
                raise
            raise Conflict(f"{provider.name} is already connected.") from error

    params: list[tuple[str, str]] = [
        ("response_type", "code"),
        ("client_id", client.client_id),
        ("redirect_uri", _redirect_uri(settings)),
        ("state", state),
        ("code_challenge", code_challenge),
        ("code_challenge_method", "S256"),
    ]
    if provider.token_style == "slack_user":  # noqa: S105 - a style tag, not a secret
        # Slack's `scope` provisions a bot; the employee's own visibility asks via
        # user_scope, and the exchange reads authed_user accordingly.
        params.append(("user_scope", " ".join(provider.scopes)))
    else:
        params.append(("scope", " ".join(provider.scopes)))
    # Provider-declared knobs (Google's access_type=offline, Atlassian's audience),
    # sent only where declared instead of sprayed across all eleven providers.
    params.extend(provider.extra_authorize_params)
    query = urlencode(params)
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
    single-use — spent before the exchange, **on a session that commits independently
    of the request** (the same design as `spend_search_budget`): the request rolls back
    as one transaction, so a spend taken on it would be undone by exactly the failures
    that matter — a provider 503 during the exchange would hand the state back its
    life, replayable until it expires. A replayed callback must find nothing whatever
    happened to the first one.
    """
    async with org_session(org_id) as spend:
        row = (
            await spend.execute(
                text(
                    "SELECT id, provider, user_id, oauth_code_verifier FROM connections "
                    "WHERE oauth_state = :state AND status = 'connecting' "
                    "AND (oauth_state_expires_at IS NULL OR oauth_state_expires_at > now()) "
                    "FOR UPDATE"
                ),
                {"state": state},
            )
        ).first()
        if row is None or row.user_id != user_id:
            # Not the owner, or a stale, expired or forged state: identical answer,
            # nothing to probe. Raising rolls the spend session back — a refusal that
            # never reached a provider leaves the owner's attempt intact.
            raise NotFound("That connection attempt was not found. Start again from Integrations.")

        provider = PROVIDERS[row.provider]
        client = oauth_client_for(row.provider)
        fernet = _fernet()
        if client is None or fernet is None:
            raise ServiceUnavailable("This integration is no longer configured.")

        # Spend the state — and the PKCE verifier with it. The row lock above makes
        # two concurrent callbacks serialize: the loser re-evaluates the predicate
        # after this commits, matches nothing, and answers 404.
        await spend.execute(
            text(
                "UPDATE connections SET oauth_state = NULL, oauth_code_verifier = NULL, "
                "oauth_state_expires_at = NULL, updated_at = now() WHERE id = :id"
            ),
            {"id": row.id},
        )

    grant = await transport.exchange_code(
        provider,
        client,
        code=code,
        redirect_uri=_redirect_uri(settings),
        code_verifier=row.oauth_code_verifier,
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

    # The callback just PROVED this subject belongs to the caller — the identity
    # endpoint answered for the token minted moments ago. Linking it is what turns a
    # connection's future content into something its owner can actually see (ADR 0014):
    # ACL rows written by the fetcher name {namespace}:{subject}, and this row is the
    # only thing that resolves the caller to that principal. Fail-closed on conflict.
    await link_verified_subject(
        session,
        org_id=org_id,
        user_id=user_id,
        source_system=SourceSystem(provider.acl_namespace),
        subject=identity.subject,
    )

    await _audit(
        session, org_id=org_id, actor_id=user_id, action="connection.connected", resource_id=row.id
    )
    return _view(updated)


async def abandon_callback(session: AsyncSession, *, user_id: UUID, state: str) -> str | None:
    """A provider denial ends the attempt: spend the state and mark the row honestly.

    The person clicked Deny (or the provider refused the grant), so no code arrives —
    only an error. The state is spent exactly as a completed callback spends it, because
    a denied attempt must be as unreplayable as a finished one, and the row goes to
    `error` so the catalogue says what happened instead of showing "connecting" for
    ever. Restarting is one click: `start_connection` reuses an `error` row.

    Fail-closed on the caller: only the owner's own connecting row matches, so a state
    carried into someone else's session clears nothing. Returns the provider id when a
    row was spent, for the redirect to name — and None when nothing matched, which is
    deliberately not an error: the browser is mid-redirect and a refusal page would
    strand it.
    """
    row = (
        await session.execute(
            text(
                "UPDATE connections SET oauth_state = NULL, oauth_code_verifier = NULL, "
                "oauth_state_expires_at = NULL, status = 'error', "
                "last_error_kind = 'authorization_denied', updated_at = now() "
                "WHERE oauth_state = :state AND user_id = :user AND status = 'connecting' "
                "RETURNING provider"
            ),
            {"state": state, "user": user_id},
        )
    ).first()
    return str(row.provider) if row is not None else None


async def _revoke_upstream_best_effort(
    session: AsyncSession, transport: OAuthTransport, *, connection_id: UUID, provider_id: str
) -> None:
    """Ask the provider to kill the grant before the local ciphertext is deleted.

    Best-effort by contract: the authority is the local deletion (a credential JUTSU
    cannot read is a credential JUTSU cannot use), and a provider that is down must
    never stop a person from disconnecting. The token is decrypted only here, held
    only for the one revocation call, and never returned.
    """
    provider = PROVIDERS.get(provider_id)
    client = oauth_client_for(provider_id)
    fernet = _fernet()
    if provider is None or provider.revoke_url is None or client is None or fernet is None:
        return
    ciphertext = (
        await session.execute(
            text("SELECT access_token_enc FROM connection_credentials WHERE connection_id = :id"),
            {"id": connection_id},
        )
    ).scalar_one_or_none()
    if ciphertext is None:
        return
    try:
        access_token = fernet.decrypt(bytes(ciphertext)).decode("utf-8")
    except InvalidToken:
        # A rotated Fernet key cannot decrypt old ciphertext; local deletion still
        # proceeds and the provider-side grant dies at its own expiry.
        return
    try:
        await transport.revoke_upstream(provider, client, access_token=access_token)
    except Exception:
        # Swallowed HERE, not trusted to the transport: "best effort" is this
        # function's contract, and whatever the provider call throws, the local
        # deletion that follows is the authority.
        return


async def disconnect_own(
    session: AsyncSession,
    transport: OAuthTransport,
    *,
    org_id: UUID,
    user_id: UUID,
    connection_id: UUID,
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
                "updated_at = now(), oauth_state = NULL, oauth_code_verifier = NULL "
                "WHERE id = :id AND user_id = :user AND status != 'disconnected' "
                "RETURNING id, provider"
            ),
            {"id": connection_id, "user": user_id},
        )
    ).first()
    if updated is None:
        raise NotFound("That connection was not found.")

    await _revoke_upstream_best_effort(
        session, transport, connection_id=connection_id, provider_id=updated.provider
    )
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
    transport: OAuthTransport,
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
                "SELECT c.user_id, c.provider, ur.role_key FROM connections c "
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
            "updated_at = now(), oauth_state = NULL, oauth_code_verifier = NULL "
            "WHERE id = :id"
        ),
        {"id": connection_id},
    )
    await _revoke_upstream_best_effort(
        session, transport, connection_id=connection_id, provider_id=row.provider
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

    Enqueues a real row in the same durable queue ingestion uses; the worker's
    `connector.sync` handler claims it from there. The idempotency key is the
    connection's identity (org-qualified, like every worker-side key), so a queue
    can hold at most one sync per connection — and a *finished* one, including a
    failed one, is reopened rather than shadowing the key for ever. Reopening a
    failure is safe precisely here because each attempt is one human click: the
    infinite-retry loop that makes the walk refuse to reopen failures cannot happen
    when a person is the loop.

    Returns the id of the row that will actually run — never a fresh UUID that
    names nothing.
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

    key = f"connector.sync:{org_id}:{connection_id}"
    inserted = (
        await session.execute(
            text(
                "INSERT INTO jobs (id, org_id, kind, state, idempotency_key, payload_json) "
                "VALUES (:id, :org, 'connector.sync', 'pending', :key, cast(:payload AS jsonb)) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id"
            ),
            {
                "id": uuid4(),
                "org": str(org_id),
                "key": key,
                "payload": f'{{"connection_id": "{connection_id}"}}',
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
            # Already queued or running: that in-flight job IS this sync.
            existing = (
                await session.execute(
                    text("SELECT id FROM jobs WHERE idempotency_key = :key"), {"key": key}
                )
            ).first()
            if existing is None:  # pragma: no cover - insert/update/select race
                raise ServiceUnavailable("The sync queue is briefly contended. Try again.")
            job_id = UUID(str(existing.id))
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
                f"SELECT {_CONNECTION_COLUMNS_WITH_COUNT} FROM connections c "  # noqa: S608
                "WHERE c.user_id = :user AND c.status != 'disconnected' ORDER BY c.provider"
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
