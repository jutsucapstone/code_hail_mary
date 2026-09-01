"""The OAuth provider registry — shared vocabulary for the API and the worker.

The API mints authorize URLs and completes callbacks; the worker refreshes tokens and
fetches content. Both must agree on token URLs, scopes and quirks, so the registry
lives here rather than in either app. It is data about *providers*; everything about a
*connection* (state, credentials, cursors) stays in the database.

Two rules are enforced by shape rather than review:

* **Read-only scopes only (§4.8).** Every scope tuple below is read-only at the
  provider. GitHub's classic OAuth app model deserves its note: `repo` (the only scope
  that reads private repositories) also writes, so it is refused — private-repo
  ingestion for GitHub requires a GitHub App installation, which grants read-only
  content permissions properly, and is a different flow. Until then GitHub syncs what
  `read:user`/`read:org` can see.
* **Provider quirks are declared, not special-cased at call sites.** Google's
  `access_type=offline` and Atlassian's `audience` live in
  `extra_authorize_params`; Slack's user-token flow is a `token_style`, and the
  callers branch on the declaration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

__all__ = [
    "GROUP_LABELS",
    "PROVIDERS",
    "OAuthClient",
    "Provider",
    "oauth_client_for",
]


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
    #: Provider-specific authorize-URL parameters. Declared here so a Google-only knob
    #: is never sent to the other ten providers.
    extra_authorize_params: tuple[tuple[str, str], ...] = ()
    #: "standard" reads access_token off the token response; "slack_user" reads the
    #: authed_user block (the employee's own token, not a bot's) and asks for scopes
    #: via user_scope.
    token_style: str = "standard"  # noqa: S105 - a parsing style tag, not a secret
    #: How the token endpoint authenticates the *client*: "body" posts client_id and
    #: client_secret as form fields (everyone else); "basic" sends an HTTP Basic
    #: Authorization header and keeps them out of the body (Zoom requires this, and
    #: RFC 6749 forbids using both at once).
    token_auth: str = "body"  # noqa: S105 - an auth placement tag, not a secret
    #: Which source_identities namespace this provider's proven subject belongs to
    #: (ADR 0014). Four Google products share one subject (the OIDC sub), three
    #: Microsoft products share the Graph object id; the namespaces mirror that.
    acl_namespace: str = ""
    #: Where an access token can be revoked upstream at disconnect, and how:
    #: "post_token" (Google), "bearer_post" (Slack auth.revoke), "github_grant"
    #: (DELETE the whole grant with basic auth), "basic_post_token" (Zoom: the token
    #: as a form field under the client's Basic header). None: the provider offers no
    #: revocation endpoint and local deletion is the whole story.
    revoke_url: str | None = None
    revoke_style: str | None = None


_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, not a secret
_GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
_GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"
_MS_AUTH = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_MS_TOKEN = "https://login.microsoftonline.com/common/oauth2/v2.0/token"  # noqa: S105
_MS_USERINFO = "https://graph.microsoft.com/v1.0/me"
_ATLASSIAN_AUTH = "https://auth.atlassian.com/authorize"
_ATLASSIAN_TOKEN = "https://auth.atlassian.com/oauth/token"  # noqa: S105
_ATLASSIAN_ME = "https://api.atlassian.com/me"


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
        acl_namespace="gmail",
        # offline -> a refresh token; consent -> Google reissues one on reconnect
        # instead of silently omitting it the second time.
        extra_authorize_params=(("access_type", "offline"), ("prompt", "consent")),
        revoke_url=_GOOGLE_REVOKE,
        revoke_style="post_token",
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
        acl_namespace="m365",
        # No revocation endpoint for v2 access tokens; they expire on their own and
        # the refresh token dies with the local ciphertext.
    )


def _atlassian(id_: str, name: str, description: str, *scopes: str) -> Provider:
    return Provider(
        id=id_,
        name=name,
        group="engineering",
        description=description,
        authorize_url=_ATLASSIAN_AUTH,
        token_url=_ATLASSIAN_TOKEN,
        userinfo_url=_ATLASSIAN_ME,
        scopes=(*scopes, "offline_access"),
        acl_namespace=id_,
        # audience is required for api.atlassian.com tokens; consent prompts the
        # grant screen that actually issues offline_access.
        extra_authorize_params=(("audience", "api.atlassian.com"), ("prompt", "consent")),
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
            # auth.test answers for any token; users.identity would demand the
            # Sign-in-with-Slack scopes this flow does not request.
            userinfo_url="https://slack.com/api/auth.test",
            # User-token scopes: the employee's own visibility, never a bot's. Sent as
            # user_scope on the authorize URL (token_style below), because Slack's
            # `scope` parameter provisions a bot.
            scopes=("channels:history", "channels:read", "users:read"),
            acl_namespace="slack",
            token_style="slack_user",  # noqa: S106 - a parsing style tag, not a secret
            revoke_url="https://slack.com/api/auth.revoke",
            revoke_style="bearer_post",
        ),
        _atlassian(
            "jira",
            "Jira",
            "Issues and projects you can already see.",
            "read:jira-work",
            "read:jira-user",
        ),
        _atlassian(
            "confluence",
            "Confluence",
            "Pages and spaces you can already read.",
            "read:confluence-content.all",
            "read:confluence-user",
        ),
        Provider(
            id="zoom",
            name="Zoom",
            group="communication",
            description="Cloud recordings and transcripts of meetings you hosted.",
            authorize_url="https://zoom.us/oauth/authorize",
            token_url="https://zoom.us/oauth/token",  # noqa: S106 - endpoint, not a secret
            userinfo_url="https://api.zoom.us/v2/users/me",
            # Zoom binds scopes at app registration (the Marketplace app's Scopes tab)
            # and its authorize URL carries no scope parameter, so the tuple is empty
            # and the URL builder omits it. §4.8 is enforced by what the app registers:
            # user:read:user plus the cloud_recording read scopes (or classic
            # user:read + recording:read) and nothing that writes.
            scopes=(),
            acl_namespace="zoom",
            token_auth="basic",  # noqa: S106 - an auth placement tag, not a secret
            revoke_url="https://zoom.us/oauth/revoke",
            revoke_style="basic_post_token",
        ),
        Provider(
            id="github",
            name="GitHub",
            group="engineering",
            description="Repositories, issues and pull requests you can already see.",
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",  # noqa: S106 - endpoint
            userinfo_url="https://api.github.com/user",
            # repo:status was here once and is gone deliberately: GitHub documents it
            # as read/WRITE on commit statuses, and §4.8 admits no write scope for any
            # feature. Private-repo *content* would need `repo` (also write) — refused;
            # a GitHub App installation is the read-only path to private content.
            scopes=("read:user", "read:org"),
            acl_namespace="github",
            revoke_url="https://api.github.com/applications/{client_id}/grant",
            revoke_style="github_grant",
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
    client_secret: str = field(repr=False)


def oauth_client_for(provider_id: str) -> OAuthClient | None:
    """The deployment's client registration for one provider, or None.

    Read from the environment at call time rather than frozen into settings: these are
    per-deployment operational secrets (Secret Manager in production, `.env` locally),
    and their absence is a meaningful state the catalogue reports rather than an error.
    """
    prefix = f"JUTSU_OAUTH_{provider_id.upper()}"
    client_id = os.environ.get(f"{prefix}_CLIENT_ID", "").strip()
    client_secret = os.environ.get(f"{prefix}_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        return OAuthClient(client_id=client_id, client_secret=client_secret)
    return None
