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
     └── deploy                     api · worker · web
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

Runtime — reach Cloud SQL and read its own secrets, and nothing else:

```bash
for role in roles/cloudsql.client roles/secretmanager.secretAccessor; do
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
gcloud sql instances create jutsu --database-version=POSTGRES_16 --region="$REGION" --tier=db-g1-small
gcloud sql databases create jutsu --instance=jutsu
gcloud sql users create jutsu-app --instance=jutsu --password="$(openssl rand -base64 32)"
```

`pgvector` is an extension, not a flag — connect once and
`CREATE EXTENSION IF NOT EXISTS vector;`. Then apply
`infra/docker/initdb/01-app-role.sql`, which is what creates the restricted role with the
right attributes.

### 6. Secrets

Referenced by the pipeline, never passed through it. `--set-secrets` mounts them at
container start, so nothing sensitive appears in the workflow, in a Cloud Run environment
variable, or in `gcloud run services describe` output.

```bash
printf '%s' 'postgresql+asyncpg://jutsu_app:PASSWORD@/jutsu?host=/cloudsql/PROJECT:REGION:jutsu' \
  | gcloud secrets create jutsu-database-url --data-file=-

python -c "import secrets; print(secrets.token_urlsafe(32))" \
  | gcloud secrets create jutsu-email-pepper --data-file=-

printf '%s' 'redis://HOST:6379' | gcloud secrets create jutsu-redis-url --data-file=-
```

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
  --set-secrets="MIGRATION_DATABASE_URL=jutsu-migration-url:latest" \
  --command=uv \
  --args="run,--package,jutsu-db,alembic,-c,packages/db/alembic.ini,upgrade,head"
```

It needs `MIGRATION_DATABASE_URL` (the owner), not `DATABASE_URL` — `jutsu_app` cannot run
DDL, by design.

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

**Variables** (same page → Variables). These are not secrets and are deliberately not
stored as such — `NEXT_PUBLIC_SITE_URL` is compiled into the client bundle and served to
every visitor, so treating it as a secret would be theatre:

| Name | Value |
|---|---|
| `NEXT_PUBLIC_SITE_URL` | `https://jutsu.dev` |
| `JUTSU_API_ORIGIN` | the API service's Cloud Run URL |

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
