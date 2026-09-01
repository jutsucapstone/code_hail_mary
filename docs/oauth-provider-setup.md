# Live connections: what the owner configures, per provider

Everything in the connection path is implemented and tested; what remains is external:
each provider needs an OAuth app registered in its developer console by someone who
owns those accounts. Nothing here is pasted into chat or committed — values go into
`.env` locally and Secret Manager in staging/production.

**The redirect URI is the same for every provider** (exact, no trailing slash):

    {JUTSU_APP_URL}/api/jutsu/v1/connections/callback

    dev:  http://localhost:3210/api/jutsu/v1/connections/callback
    prod: https://jutsu.co.in/api/jutsu/v1/connections/callback

**Every provider needs two environment variables** (the credentials from its console):

    JUTSU_OAUTH_{PROVIDER_ID_UPPERCASED}_CLIENT_ID
    JUTSU_OAUTH_{PROVIDER_ID_UPPERCASED}_CLIENT_SECRET

**Once, for the deployment** (already generated for dev): `JUTSU_CONNECTION_KEY`, the
Fernet key encrypting stored tokens. Generate with
`uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

Scopes below are what the code requests — read-only, all of them (§4.8). Consent
screens must list exactly these; requesting more in the console is harmless but
misleading, requesting fewer breaks the flow.

---

## Google (google_drive, gmail, google_calendar, google_meet)

- **Console**: https://console.cloud.google.com/apis/credentials (project `jutsu-506513` or a dedicated one)
- **Application type**: OAuth client ID → Web application
- **One client can serve all four providers** — set all four env-var pairs to the same values, or register separate clients per product for separate consent branding.
- **Enable APIs**: Drive API, Gmail API, Calendar API, Google Meet API
- **OAuth consent screen**: External (or Internal for a Workspace org). The sensitive/restricted scopes below put an External app into *Testing* mode (100 users) until Google verifies it — fine for a pilot; verification (with domain proof and a privacy policy URL) is the production path. Restricted scopes (Gmail, Drive) additionally require a CASA security assessment for verification.
- **Scopes**: `openid email` plus per product:
  - google_drive: `https://www.googleapis.com/auth/drive.readonly` (restricted)
  - gmail: `https://www.googleapis.com/auth/gmail.readonly` (restricted)
  - google_calendar: `https://www.googleapis.com/auth/calendar.readonly` (sensitive)
  - google_meet: `https://www.googleapis.com/auth/meetings.space.readonly` (sensitive)
- **Billing**: none for OAuth itself.

## Microsoft (onedrive, teams, sharepoint)

- **Console**: https://entra.microsoft.com → App registrations → New registration
- **Supported account types**: "Accounts in any organizational directory and personal Microsoft accounts" (the code uses the `/common` endpoint)
- **Redirect URI**: type Web, the URI above
- **Client secret**: Certificates & secrets → New client secret (note its expiry — Entra secrets expire, max 24 months; rotation is an env-var update)
- **API permissions** (Microsoft Graph, *Delegated*): `openid`, `email`, `offline_access`, plus `Files.Read` (onedrive), `Chat.Read` + `ChannelMessage.Read.All` (teams), `Sites.Read.All` (sharepoint). `ChannelMessage.Read.All` and `Sites.Read.All` need admin consent in the tenant that connects.
- One registration can serve all three providers (same env values three times).

## Slack (slack)

- **Console**: https://api.slack.com/apps → Create New App → From scratch
- **OAuth & Permissions** → Redirect URLs: the URI above. **User Token Scopes** (NOT Bot Token Scopes): `channels:history`, `channels:read`, `users:read`.
- The code runs a user-token flow (`user_scope`); adding bot scopes provisions a bot nobody wants.
- Optional: enable token rotation (the refresh path handles rotated tokens).

## Atlassian (jira, confluence)

- **Console**: https://developer.atlassian.com/console/myapps → Create → OAuth 2.0 integration
- **Authorization**: add the callback URI. **Permissions**: Jira API → `read:jira-work`, `read:jira-user`; Confluence API → `read:confluence-content.all`, `read:confluence-user`. Refresh tokens arrive via the `offline_access` scope the code already requests; Atlassian *rotates* them — handled.
- One app can serve both providers (same env values for jira and confluence).

## GitHub (github)

- **Console**: https://github.com/settings/developers → OAuth Apps → New OAuth App (or an org-owned app)
- **Authorization callback URL**: the URI above.
- **Scopes** are requested at runtime (`read:user`, `read:org`) — nothing to configure in the console beyond the callback.
- Reach is public repositories only: the classic `repo` scope also writes and is refused (§4.8). Private-repo content read-only requires a **GitHub App** installation — a different flow, deliberately not faked with a write-capable scope.

---

## Verifying a provider once its credentials are set

1. Put the two env vars in `.env`, restart `make api` (env is read per call, but the
   dev server was started with `--env-file`).
2. Sign in as any member → Integrations → the provider's card now shows Connect.
3. Connect → provider consent → land back on Integrations with a success toast; the
   card shows the account label and the linked identity appears under the admin's
   Source identities view (`linked_by = oauth_connection`).
4. Sync now → the Jobs view shows `connector.sync` complete and an `ingest.source`
   walk; documents appear under Knowledge sources; "Documents indexed" counts up;
   Ask JUTSU can cite the new content once embedding runs.
5. `make worker` must be running for step 4 (the API rings the arq doorbell; the
   worker drains the org).

## Production notes (Cloud Run)

Every secret lives in Secret Manager, and there are two ways one gets mounted.
`deploy.yml` carries the deployment-wide set (`jutsu-anthropic-api-key`,
`jutsu-connection-key`, database URL, pepper, SMTP) and re-asserts it on every push.
The per-provider pairs are **not** in the pipeline — an operator mounts each one once,
and the pipeline preserves it:

```bash
printf '%s' 'the-client-id'     | gcloud secrets create jutsu-oauth-github-client-id --data-file=-
printf '%s' 'the-client-secret' | gcloud secrets create jutsu-oauth-github-client-secret --data-file=-

gcloud run services update jutsu-api --region=asia-south1 \
  --update-secrets "JUTSU_OAUTH_GITHUB_CLIENT_ID=jutsu-oauth-github-client-id:latest,JUTSU_OAUTH_GITHUB_CLIENT_SECRET=jutsu-oauth-github-client-secret:latest"
```

This works because the deploy uses `--update-secrets`, which merges with the previous
revision's secret set rather than replacing it — a hand-mounted pair survives every
push. Rotation is a `gcloud secrets versions add`; the mount reads `latest`.
`GOOGLE_CLOUD_PROJECT` and `VERTEX_LOCATION` are plain env vars; Vertex auth is the
attached service account (`roles/aiplatform.user`), no key files.
