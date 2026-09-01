# Deploying to GCP

What `.github/workflows/deploy.yml` expects to exist, and how to create it. Nothing here
runs automatically — the pipeline assumes the infrastructure is already provisioned, and
fails loudly rather than creating cloud resources as a side effect of a push.

Until this is set up, the pipeline is inert: CI still gates every pull request, and the
deploy job fails at authentication rather than doing something surprising.

---

## The shape

```
push to main
     │
     ├── ci.yml (reusable)          lint · typecheck · tests · build
     │                              the deploy gates on this exact job, not a copy of it
     ├── build                      three images, tagged with the commit SHA
     │                              pushed to Artifact Registry
     ├── migrate                    Cloud Run job, alembic upgrade head
     │                              runs as the OWNER, before any service is deployed
     └── deploy                     api · reaper job · web
                                    then curls the web URL and rolls back if it never 200s
```

**Migrations run before the services, and that ordering is only safe for additive
changes.** The new revision expects the new schema, so deploying first means every
request in between hits code whose tables do not exist. The cost is the mirror image: a
column dropped in a migration breaks the revision still serving traffic. Destructive
changes therefore go out as two deploys — stop using the column, ship, then drop it.

---

## One-time GCP setup

Set your project and region once:

```bash
export PROJECT_ID=jutsu-capstone
export REGION=asia-south1
export REPO=jutsucapstone/code_hail_mary
gcloud config set project "$PROJECT_ID"
```

### 1. Enable the APIs

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com iamcredentials.googleapis.com
```

### 2. Artifact Registry

The repository name (`jutsu`) and region must match `REPOSITORY` and `REGION` in
`deploy.yml`.

```bash
gcloud artifacts repositories create jutsu --repository-format=docker --location="$REGION" --description="JUTSU service images"
```

### 2b. Vertex AI, and the budget that has to exist first

Embedding is the first thing in JUTSU that costs money per unit of work, so the guardrail
goes in before the capability does — §20 asks for budget alerts "day one, not after the
first bill", and a budget created afterwards is a budget that was not there for the run
that mattered.

```bash
gcloud services enable aiplatform.googleapis.com --project "$PROJECT_ID"
gcloud services enable billingbudgets.googleapis.com --project "$PROJECT_ID"

# Confirm a budget EXISTS rather than assuming it does. An empty list here is a finding.
gcloud billing budgets list --billing-account="$BILLING_ACCOUNT"
```

The runtime service account needs `roles/aiplatform.user` and nothing wider. It is granted
below with the other runtime roles; called out here because the API being enabled and the
account being able to use it are two different things, and the second failure surfaces as
a 403 in a worker rather than at deploy time.

**Region.** `VERTEX_LOCATION` is a data-residency decision, not a latency one (§20 —
"Vertex AI, regional"). `gemini-embedding-001`, `text-embedding-004` and
`text-multilingual-embedding-002` were all verified callable in `asia-south1` on
2026-08-27. Moving to another region to chase availability moves customer text with it.

**No key file.** Cloud Run supplies the attached service account and
`packages/retrieval` uses Application Default Credentials. `GOOGLE_APPLICATION_CREDENTIALS`
stays empty in every deployed environment; a key on disk is a credential that can be
copied.

### 3. Two service accounts, not one

The account that *deploys* and the account the services *run as* are separate on purpose.
A single account would mean a compromised running container holds the permission to push
images and deploy revisions.

```bash
gcloud iam service-accounts create jutsu-deployer --display-name="GitHub Actions deployer"
gcloud iam service-accounts create jutsu-runtime  --display-name="JUTSU Cloud Run runtime"
```

Deployer — push images, deploy revisions, run the migration job:

```bash
for role in roles/run.admin roles/artifactregistry.writer roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:jutsu-deployer@${PROJECT_ID}.iam.gserviceaccount.com" --role="$role"
done
```

Runtime — reach Cloud SQL, read its own secrets, and call Vertex AI. Nothing else:

```bash
for role in roles/cloudsql.client roles/secretmanager.secretAccessor roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:jutsu-runtime@${PROJECT_ID}.iam.gserviceaccount.com" --role="$role"
done
```

### 4. Workload Identity Federation

**No service-account key is ever downloaded.** GitHub mints a short-lived OIDC token,
Google exchanges it for an access token scoped to this repository, and it expires with the
job. A JSON key in repository secrets would be a permanent credential that outlives every
person who can read it — §4.10 rules that out.

```bash
gcloud iam workload-identity-pools create github --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github \
  --location=global --workload-identity-pool=github \
  --display-name="GitHub" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository == '${REPO}'"
```

The `--attribute-condition` is load-bearing. Without it **any** GitHub repository on the
internet can exchange a token for your credentials — it is the difference between "our CI
can deploy" and "CI can deploy".

Then let only this repository impersonate the deployer:

```bash
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"

gcloud iam service-accounts add-iam-policy-binding \
  "jutsu-deployer@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${REPO}"
```

### 5. Cloud SQL, with two roles

The application connects as `jutsu_app` (`NOSUPERUSER NOBYPASSRLS`) and migrations run as
the owner. This is not tidiness: **a superuser bypasses row-level security
unconditionally**, so pointing the app at the owner would leave every tenant-isolation
policy inert while every isolation test still passed.

```bash
# --edition=ENTERPRISE is required. The default is ENTERPRISE_PLUS, which rejects
# shared-core tiers like db-g1-small outright — and costs considerably more.
gcloud sql instances create jutsu --database-version=POSTGRES_16 --edition=ENTERPRISE \
  --region="$REGION" --tier=db-g1-small --storage-size=10GB --storage-auto-increase
gcloud sql databases create jutsu --instance=jutsu

# jutsu_app, with an underscore. The DSN and every grant in migration 0001 spell it that
# way; `jutsu-app` creates a role nothing references and the app fails to authenticate.
gcloud sql users create jutsu_app --instance=jutsu --password="$(openssl rand -base64 32)"
```

Then connect as `postgres` and run three things. **`infra/docker/initdb/01-app-role.sql`
does not apply here** — it is written for a local Postgres where the bootstrap role is a
superuser, and half of it fails on Cloud SQL:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- NOSUPERUSER is deliberately absent: Cloud SQL grants superuser to nobody, so the ALTER
-- fails with "only roles with the SUPERUSER attribute may change it" — and the property
-- is already true. NOBYPASSRLS is the one that matters and is already the default; set it
-- explicitly anyway, because the whole tenancy guarantee rests on it.
ALTER ROLE jutsu_app NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
GRANT CONNECT ON DATABASE jutsu TO jutsu_app;
GRANT USAGE ON SCHEMA public TO jutsu_app;

-- Without this, migration 0002 dies on `CREATE SCHEMA auth AUTHORIZATION jutsu_auth`
-- with "must be able to SET ROLE jutsu_auth". On vanilla Postgres the migration owner is
-- a superuser and may assign ownership freely; on Cloud SQL it must be a *member* of the
-- role. Grant it to postgres ONLY — jutsu_app must never be a member, or it could
-- `SET ROLE jutsu_auth` and read every tenant's rows (migration 0002 says so at length).
GRANT jutsu_auth TO postgres;
```

`jutsu_auth` itself is created by migration 0002, so run that grant after a first failed
migration attempt, or create the role by hand first.

Statements run one at a time. `psql -c "a; b; c"` executes them in a single transaction,
so one failure silently rolls back the ones that succeeded — which is how `CREATE
EXTENSION` can report success and leave no extension behind.

### 6. Secrets

Referenced by the pipeline, never passed through it. The deploy mounts them from Secret
Manager at container start with `--update-secrets` — the merging form, so hand-mounted
secrets survive a push — and nothing sensitive appears in the workflow, in a Cloud Run
environment variable, or in `gcloud run services describe` output.

```bash
# The app role, for the services. NOSUPERUSER NOBYPASSRLS — see §5.
printf '%s' 'postgresql+asyncpg://jutsu_app:PASSWORD@/jutsu?host=/cloudsql/PROJECT:REGION:jutsu' \
  | gcloud secrets create jutsu-database-url --data-file=-

# The owner, for the migration job (§7). `jutsu_app` cannot run DDL, by design.
printf '%s' 'postgresql+asyncpg://jutsu:PASSWORD@/jutsu?host=/cloudsql/PROJECT:REGION:jutsu' \
  | gcloud secrets create jutsu-migration-url --data-file=-

python -c "import secrets; print(secrets.token_urlsafe(32))" \
  | gcloud secrets create jutsu-email-pepper --data-file=-

# Answers and extraction run on the Claude API. Absent, /v1/ask refuses with 503 and
# extraction jobs are never enqueued — honest unavailability, not a crash.
printf '%s' 'sk-ant-xxxxxxxxxxxxxxxx' | gcloud secrets create jutsu-anthropic-api-key --data-file=-

# Fernet key encrypting provider OAuth tokens at rest. Piped straight from the
# generator, so the value never touches the shell history or the screen.
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
  | gcloud secrets create jutsu-connection-key --data-file=-
```

There is deliberately **no `jutsu-redis-url`**. Production has no Redis — the reaper is
a scheduled Cloud Run job for exactly that cost reason (§8), and the deploy mounts no
`REDIS_URL`: sync jobs enqueue durable rows that wait, visible on the Jobs page, and the
mount lands together with the worker slice that would hear the doorbell.

The per-provider OAuth pairs (`jutsu-oauth-github-client-id` / `...-secret`, …) are not
listed here or in the pipeline. An operator mounts each pair once with
`gcloud run services update --update-secrets`, and the deploy — which also uses
`--update-secrets`, the merging form — preserves them across every push. See
`docs/oauth-provider-setup.md`.

### 6a. The mail transport

Passwordless sign-in cannot deliver a code without one, so **production refuses to start
until both of these exist** — `get_settings` raises rather than falling back to a
transport that prints to stdout and authenticates nobody.

Delivery is **Resend**, reached over SMTP rather than its HTTP API — the transport is an
interface, and speaking submission keeps every other provider a configuration change
rather than a rewrite.

Resend authenticates with an **API key, not a mailbox**: the username is the literal
string `resend` and the password is the key. There is no `RESEND_API_KEY` variable; the
key *is* `SMTP_PASSWORD`.

```bash
printf '%s' 'resend' | gcloud secrets create jutsu-smtp-username --data-file=-

# The Resend API key, from https://resend.com/api-keys. A send-only key is enough and is
# what this deployment uses — it cannot read domains or account settings, so a leak
# cannot be turned into a configuration change.
printf '%s' 're_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' | gcloud secrets create jutsu-smtp-password --data-file=-
```

`SMTP_HOST` and `SMTP_PORT` default to `smtp.resend.com:587` and need no secret.

**`SMTP_FROM` is not optional with this provider**, and the API refuses to start without
it. It defaults to the username, which is correct only where the username is a mailbox;
here that would yield a From header of `resend`, rejected at the first send — after the
service has started, passed its health check and told a registrant to check their email.

It takes a bare address or a display form, and production sends the display form:

```
SMTP_FROM=JUTSU <noreply@jutsu.co.in>
```

The name is presentation only. `smtplib` derives the envelope sender by parsing the
header, so `MAIL FROM` remains `noreply@jutsu.co.in` and SPF and DKIM alignment are
unaffected. The address is **outbound only and needs no mailbox behind it** — nothing
ever delivers to it.

The address must sit on a domain verified with Resend. `jutsu.co.in` is, via
`resend._domainkey.jutsu.co.in` for DKIM and a `send.jutsu.co.in` subdomain carrying
`v=spf1 include:amazonses.com ~all` and an MX to Resend's feedback host. DMARC at
`_dmarc.jutsu.co.in` is `p=quarantine` with relaxed alignment, which both the DKIM `d=`
and the envelope sender satisfy. Resend rejects an unverified sender domain outright, so
a send that is accepted is itself the verification check.

**If the secret already exists, add a version — do not re-`create`.** `secrets create`
fails on an existing name, and the deployed revision reads `latest`, so a new version is
picked up on the next request without a redeploy:

```bash
printf '%s' 'xxxxxxxxxxxxxxxx'   | gcloud secrets versions add jutsu-smtp-password --data-file=-
```

A wrong or placeholder password is not a startup failure — the credential is only exercised
when mail is first sent, so the service comes up healthy and `/readyz` passes. The symptom
is a 500 on `POST /v1/orgs/register` with this in the logs, and a response body carrying
nothing beyond the standard envelope:

```
smtplib.SMTPAuthenticationError: (535, b'5.7.8 Username and Password not accepted.')
```

Nothing is persisted when this happens. The budget is spent and the challenge issued before
the send, and the staged registration is written after it, so the exception rolls the whole
request transaction back — no organisation, no pending row, no stored name or address. The
registrant sees a failure and can retry once the credential is fixed.

Gmail's free tier caps sending at roughly 500 messages a day. Ample for a pilot, and the
reason the transport is an interface rather than an inlined SMTP call: moving to a
dedicated sender later is a new class, not a rewrite.

**Generate the pepper once and never rotate it casually.** It keys the HMAC standing in
for email addresses in the org-less `auth` schema; changing it orphans every existing
identity, and every account silently stops resolving.

### 7. The migration job

Created once; the pipeline only updates its image and executes it.

```bash
gcloud run jobs create jutsu-migrate \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/jutsu/api:bootstrap" \
  --region="$REGION" \
  --service-account="jutsu-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-cloudsql-instances="${PROJECT_ID}:${REGION}:jutsu" \
  --set-secrets="MIGRATION_DATABASE_URL=jutsu-migration-url:latest,DATABASE_URL=jutsu-migration-url:latest" \
  --command=alembic \
  --args="-c,packages/db/alembic.ini,upgrade,head"
```

It needs `MIGRATION_DATABASE_URL` (the owner), not the app role — `jutsu_app` cannot run
DDL, by design. `DATABASE_URL` is set to the same value because Alembic's `env.py` falls
back to it.

`--command=alembic`, not `uv`: **`uv` exists only in the build stage of the image.** The
runtime stage puts the venv on `PATH`, so the entrypoint is `alembic` itself — `uv` fails
with "executable file not found", which is what the first real migration run produced.

### 8. The reaper job and its schedule

`auth.pending_registrations` holds a name, a work address and a job title for ten minutes.
An expiry column with nothing deleting it is a comment, not a control — so something has
to run `auth.reap_expired_registrations()`.

**A scheduled job, not the arq worker.** arq's cron scheduler lives inside the process, so
running it on Cloud Run means `--min-instances=1 --no-cpu-throttling` — a container billed
continuously — plus Redis for arq to talk to. Memorystore alone costs more than every other
piece of this deployment put together, to delete a handful of rows every five minutes.

```bash
gcloud run jobs create jutsu-reap \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/jutsu/worker:bootstrap" \
  --region="$REGION" \
  --service-account="jutsu-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-cloudsql-instances="${PROJECT_ID}:${REGION}:jutsu" \
  --set-secrets="DATABASE_URL=jutsu-database-url:latest" \
  --command=python --args="-m,jutsu_worker.reap" \
  --max-retries=2 --task-timeout=5m
```

The worker image, because that is where the pipeline points this job: `deploy.yml`
repoints `jutsu-reap` at the worker image it just built on every push, so creating the
job from anything else holds for exactly one deploy. The worker image is the same build
as the API image with a different default command (`infra/docker/worker.Dockerfile` says
so at the top) — still one build to keep patched, not two that drift — and the
`--command` above overrides the default either way.

Then a schedule, and a service account allowed to invoke it:

```bash
gcloud iam service-accounts create jutsu-scheduler --display-name="Cloud Scheduler invoker"

gcloud run jobs add-iam-policy-binding jutsu-reap --region="$REGION" \
  --member="serviceAccount:jutsu-scheduler@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/run.invoker

gcloud scheduler jobs create http jutsu-reap-schedule \
  --location="$REGION" \
  --schedule="*/5 * * * *" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/jutsu-reap:run" \
  --http-method=POST \
  --oauth-service-account-email="jutsu-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
```

Every five minutes against a ten-minute TTL, so an abandoned registration's details exist
for at most about a quarter of an hour. The work is one indexed DELETE against a table
that is usually empty, and Cloud Scheduler's free tier covers three jobs.

The arq cron in `main.py` stays. It is the right shape once S8 puts a worker on Cloud Run
for the ingestion queue's own reasons — at that point the reaper is already wired into a
process that is running anyway, and this job can be deleted.


---

### 9. The custom domain

`jutsu.co.in`, fronted by a global external Application Load Balancer.

**Not a Cloud Run domain mapping.** Mappings are free and would have been the obvious
choice, but they are unavailable in `asia-south1`; the API answers `501 UNIMPLEMENTED —
Creating domain mappings is not allowed in asia-south1`. Worth knowing how that presents:
if the domain is not verified yet, the verification error is raised *first* and hides the
region error completely, so a mapping can look like it is one manual step away when it is
not possible at all. Verify the domain, then re-run the create, before believing either
error.

Firebase Hosting is the other supported route and is genuinely cheaper — free custom
domain and SSL, 10 GB storage, 360 MB/day of transfer, and it can rewrite to a Cloud Run
service in `asia-south1`. It was not taken because the daily transfer cap is a real
ceiling for a demo and the setup runs through the Firebase console rather than `gcloud`.
It remains the right answer if the load balancer's standing cost stops being worth it.

The load balancer is billed continuously — a forwarding rule is charged per hour whether
or not anything reaches it, and there are two of them here (`:80` and `:443`). This is the
one component of this deployment that costs money while idle.

**The resources, in dependency order:**

```
jutsu-lb-ip          global static IPv4, 34.36.151.92
jutsu-web-neg        serverless NEG -> Cloud Run jutsu-web (asia-south1)
jutsu-web-backend    global backend service, EXTERNAL_MANAGED, holds the NEG
jutsu-cert           Google-managed cert for jutsu.co.in + www.jutsu.co.in
jutsu-url-map        default -> backend; host www.jutsu.co.in -> 301 to the apex
jutsu-http-redirect  everything -> https://jutsu.co.in
jutsu-https-proxy    jutsu-url-map + jutsu-cert
jutsu-http-proxy     jutsu-http-redirect
jutsu-https-rule     :443 on the static IP
jutsu-http-rule      :80  on the static IP
```

IPv4 only. A second pair of forwarding rules for IPv6 doubles the standing charge, and
nothing here needs it yet.

**DNS at the registrar** (GoDaddy — the nameservers are `ns21/ns22.domaincontrol.com`):

| Type | Name | Value |
|---|---|---|
| A | `@` | `34.36.151.92` |
| A | `www` | `34.36.151.92` |

The parking A records GoDaddy installs must be **deleted**, not left alongside. DNS
round-robins across every A record for a name, so leaving them means a share of visitors
resolve to a parked page — intermittently, which is far harder to diagnose than a clean
failure. Keep the `google-site-verification` TXT record.

**The certificate provisions only after DNS resolves to the load balancer.** It cannot be
hurried; Google validates by fetching over the very records above. `PROVISIONING` is normal
and can persist for up to a day:

```bash
gcloud compute ssl-certificates describe jutsu-cert --global   --format='value(managed.status,managed.domainStatus)'
```

HTTPS returns a TLS error, not a 404, until it reports `ACTIVE`.

**A cosmetic quirk worth not chasing:** the `:80` redirect emits
`Location: https://jutsu.co.in:443/`. The port suffix is inherent to the load balancer's
`httpsRedirect` and cannot be removed from the URL map. It is the default port, browsers
treat the two as one origin, and the page's own canonical tag carries no port — so it
costs nothing beyond looking odd in a header dump.

**Verifying before DNS exists.** Point `jutsu-http-proxy` at `jutsu-url-map` for a moment
and request the IP with an explicit `Host` header; this exercises the whole chain — LB,
backend service, NEG, Cloud Run — without a certificate or a DNS record:

```bash
curl -s -H "Host: jutsu.co.in" http://34.36.151.92/ | grep canonical
```

Put the proxy back on `jutsu-http-redirect` afterwards. Every change to a proxy or URL map
takes a few minutes to reach every edge, so a wrong answer immediately after an update is
usually propagation rather than a mistake — re-check before changing anything.

**Then, and only then, Search Console.** Submit the sitemap and request indexing once the
domain answers on HTTPS. Doing it earlier asks Google to crawl a host that does not
resolve, and that is remembered for longer than it takes to do these in order. Indexing a
new domain with no inbound links takes days to weeks and cannot be forced; `site:jutsu.co.in`
shows whether it is indexed at all, which is a separate question from where it ranks.

---

## GitHub configuration

**Secrets** (Settings → Secrets and variables → Actions → Secrets):

| Name | Value |
|---|---|
| `GCP_PROJECT_ID` | `jutsu-capstone` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/NUMBER/locations/global/workloadIdentityPools/github/providers/github` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | `jutsu-deployer@PROJECT.iam.gserviceaccount.com` |
| `GCP_RUNTIME_SERVICE_ACCOUNT` | `jutsu-runtime@PROJECT.iam.gserviceaccount.com` |
| `CLOUD_SQL_INSTANCE` | `PROJECT:REGION:jutsu` |

**Variables**: none are required, and `NEXT_PUBLIC_SITE_URL` should be **deleted** if it
exists.

It used to live here. The trouble is that it is compiled into the client bundle, so it was
never a secret and never varied — one deployment, one public origin — and holding it out
of band meant the workflow could not show what it was building with. It was set to the
`run.app` hostname, which silently outranked the default beneath it, so every canonical
tag, OG URL and sitemap entry named the wrong host while the pipeline stayed green. It is
now written in `deploy.yml`, where changing it is a reviewable diff.

`JUTSU_API_URL` is optional. Left unset, the deploy job reads the URL off the API service
it deployed moments earlier, which is correct by construction and survives a fork. Set it
only to point the web app somewhere the pipeline did not just deploy.

**Environment**: create one named `production` (Settings → Environments) and add required
reviewers. The `migrate` and `deploy` jobs both target it, so a schema change waits for a
human. Everything else in the pipeline is reversible by redeploying the previous image;
the migration is the step that is not.

---

## Rollback

Images are tagged with the commit SHA and never `latest`, so a rollback is a traffic
change rather than a rebuild:

```bash
gcloud run services update-traffic jutsu-web --region="$REGION" --to-revisions=REVISION=100
```

The pipeline already does this automatically for the web service if the new revision never
returns 200. **It does not roll back the schema** — a migration that has applied stays
applied, which is why additive-only is the rule above.

---

## What is deliberately not here

No Terraform. `make deploy` is stubbed to S29 and the plan puts infrastructure-as-code in
that slice; a half-written module that disagrees with the console is worse than a runbook
that admits it is one. The commands above are the thing Terraform will encode.

No CDN, no custom domain, no autoscaling policy beyond `min`/`max` instances. Cloud Run's
defaults are adequate at pilot scale and every knob turned early is one tuned against
imaginary traffic.

---

## What the first real deployment actually needed

Recorded because every one of these cost a failed run, and none was visible from reading
the config.

**`JUTSU_ENV=prod` refuses to serve registration.** `ConsoleEmailSender` raises on
construction in production — deliberately, so mail is never silently discarded. Until a
real transport is wired, the deployed environment must be `staging`, where codes are
written to Cloud Run logs. That is honest for a demo and must not be called production.

**The API cannot be `--no-allow-unauthenticated` as things stand.** The Next proxy calls
it over the public URL with no Google credential, so a private service rejects every
request a browser makes. Closing it properly means granting the web service account
`run.invoker` and having the proxy attach an identity token.

**`/healthz` never reaches the container.** Google's frontend answers it with its own 404
page — no `x-request-id`, so our middleware never ran. Every other path, including
`/nonsense`, reaches the app. `/readyz` works and is what the platform should poll.

**Verifying data from psql shows nothing unless you set the tenant scope.** `orgs`,
`users`, `user_roles` and `terms_acceptances` are under `FORCE ROW LEVEL SECURITY`, which
subjects the table *owner* too, and Cloud SQL's `postgres` is not a superuser. A plain
`SELECT count(*) FROM orgs` returns 0 whether or not rows exist:

```sql
SELECT set_config('app.current_org_id', '<org-uuid>', false);
```

The org id is readable from `auth.jutsu_ids`, which carries no `org_id` policy. Seeing
zero rows without the scope is the isolation working, not an empty database.
