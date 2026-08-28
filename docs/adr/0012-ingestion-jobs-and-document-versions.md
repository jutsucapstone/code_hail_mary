# ADR 0012 — Postgres is the queue; Redis is a doorbell

- **Status:** accepted
- **Date:** 2026-08-28
- **Slice:** S8

## The sentence this ADR exists to make true

> **A job exists because a row exists.** Losing Redis loses throughput, never work.

## Context

§9 requires each ingestion stage to be separately queued with its own retry policy, so a
failure at one never forces another. The stack names arq with Redis in dev and Cloud Tasks
in prod, behind one interface. Neither says where the *truth* lives, and that turned out to
decide everything else.

Three properties had to hold at once, and the third is what made the first two hard:

1. **Idempotency across crashes, retries, duplicate dispatch and repeated runs.**
2. **A changed document produces a new version; an unchanged one produces nothing.**
3. **`jobs` and `sources` isolated by row-level security**, which they were not — both had
   carried `org_id` since migration 0001 with no policy over it, the `user_groups` defect of
   ADR 0010 repeating in two more tables.

## The discovery that shaped the design

Adding RLS to `jobs` makes cross-tenant discovery impossible. That was the intent, but it
also breaks the obvious way to reclaim work from a crashed worker: a sweeper has to read
`jobs` without yet knowing which tenant the orphan belongs to.

Migration 0002 already warned that `FORCE ROW LEVEL SECURITY` subjects the table's own
owner to its policies. It was verified rather than trusted — first with a superuser owner,
which proved nothing because superusers bypass RLS unconditionally, then correctly with a
non-superuser owner:

```
rows inserted                      : 2 (two tenants)
jutsu_app direct SELECT, no GUC    : 0
jutsu_app via SECURITY DEFINER fn  : 0
```

**A `SECURITY DEFINER` function over a FORCE-RLS table returns zero rows with no error.**
That is the worst failure shape available here: a sweeper that silently reclaims nothing is
indistinguishable from a sweeper with nothing to reclaim.

Four options were put to the decision-maker — a `BYPASSRLS` role, a policy carve-out with
column grants, an unscoped mirror table, or no global sweeper at all. **Option D was
chosen**: recovery is org-scoped and runs at the start of each source run.

## Decision

### 1 · Durable state in Postgres, dispatch in Redis

The `jobs` row is the work. arq carries `{org_id, job_id}` and nothing else of consequence.
A message delivered twice is refused by the lease; delivered late, harmless; never
delivered, recovered by the next source run.

**The dispatch message's `org_id` is a hint about where to look, never an authorization.**
It scopes the session; the policy then decides what that scope can see. A forged message
naming another tenant finds no row of theirs — it finds nothing in its own and returns.

### 2 · Three job kinds, because embedding costs money

`ingest.source` → `ingest.document` → `embed.document`. The split that matters is the
second: the document job is **committed** before the embedding job exists, so no embedding
failure can put a document job back into a claimable state. That is §9's requirement made
structural rather than promised, and there is a regression test that deletes the entire
corpus before failing the embedding, so a re-fetch would raise rather than merely be
unwanted.

### 3 · Three transactions per job

    claim (commits) → work → failure (a new transaction)

**The claim commits before the work starts**, and that is what makes bounded attempts
bounded. Claiming and working in one transaction looks tidier and is wrong: a failure rolls
back the `attempts + 1` too, so the job returns looking untouched and retries for ever.

**The failure is recorded in a fresh transaction** because Postgres aborts a transaction at
the first error. A handler writing `failure_kind` into the transaction that just failed
records nothing and raises something unrelated.

### 4 · Idempotency keys are identity, never time

| Key | Shape |
|---|---|
| source | `ingest.source:{org}:{source}` |
| document | `ingest.document:{org}:{source}:{external_id}` |
| embedding | `embed.document:{org}:{document_id}` |

No timestamps, no UUID4. The document key is the document's **identity, not its version** —
the content hash cannot be known before fetching, which is the stage the key schedules.
Content identity is enforced inside `persist_document`, which compares `content_hash` and
writes nothing when it matches.

The embedding key needs no hash: a new version *is* a new document id.

### 5 · A completed job is reopened by the next walk

This was a real defect, found by seeding twice and noticing the second run did no work at
all. The document key is the identity, so a completed job shadows that identifier for
ever — and no later edit would ever be ingested.

The walk therefore reopens a job in state `completed`. It does **not** reopen `failed` or
`dead_letter`: a permanent failure reopened by every sync is an infinite loop that never
reaches anybody's attention. The cost is that a document which failed permanently and has
since been fixed at the source needs an explicit requeue, and that is the safer direction.

### 6 · Versioning: partial unique index, deferrable foreign key

`uq_documents_org_source_external` becomes a unique index `WHERE superseded_by IS NULL` —
one *current* document per source identifier, with as much history behind it as the source
produces. S7 already filters superseded rows, so the read path is untouched.

`fk_documents_superseded_by` becomes **DEFERRABLE**, and that was found by testing the
design rather than reasoning about it:

| Order | Result |
|---|---|
| insert new, then supersede old | two rows with `superseded_by IS NULL` — the partial index refuses |
| supersede old, then insert new, immediate FK | references a row that does not exist — the FK refuses |
| supersede old, then insert new, **FK deferred** | correct, and never two current versions at any statement boundary |

**`downgrade` refuses to run once anything has been superseded**, because restoring the old
non-partial constraint over real version history would mean destroying it. A migration that
silently deleted a tenant's document versions to make a constraint fit would be far worse
than one that stops.

### 7 · Failure classification, not one FAILED state

`source_unavailable`, `malformed_document`, `embedding_transient`, `embedding_permanent`,
`budget_exhausted`, `internal`. An operator's first question is not "did it fail" — the
state says that — it is "can this be retried, and if not, what has to change first".

Anything unrecognised is `internal` and **retryable**, which is the safe direction: an
unknown permanent failure costs a bounded number of attempts and then dead-letters, while
an unknown failure treated as permanent silently drops work.

### 8 · Every S6 protection is inherited, not reimplemented

`embed.document` calls `embed_pending_chunks`, which selects `WHERE embedding IS NULL`. 768
dimensions, L2 normalisation, task-type split, provider token accounting, truncation
rejection, batch bounds, jittered retry and the token budget all come with it. The one
addition is an optional `document_id` filter, so a job named after one document cannot
spend another's budget.

## Consequences

- **There is no global cross-tenant sweeper.** Reclaiming expired leases happens at the
  start of each source run, inside that organisation's scope. **An organisation whose
  source is never processed again keeps its orphaned jobs.** This is the price of option D
  and it is stated here rather than discovered later.
- **The boundary for the future multi-tenant scheduler is exactly this.** That slice must
  decide how to enumerate tenants — it needs to know which sources exist in order to
  schedule them at all — and whichever mechanism it chooses will also close this gap. It is
  deliberately **not** implemented speculatively here.
- **`pii_vault` is still empty.** Masking works and is stored; the vault needs a
  key-management decision, and the original body is retained behind the ACL check, so
  nothing is lost today beyond cross-document entity resolution, which no slice needs yet.
- **No graph writes.** Nothing extracts, so an interface for extraction would be
  speculative.
- **No credentials are introduced or stored.** `LocalConnector` needs a path. Provider
  connectors bring their own credential handling in Phase 4, from Secret Manager, and
  `config_json` will hold a reference rather than a value.
- **The audit log's `outcome` is its own three-value vocabulary** — `success`, `denied`,
  `failure` — constrained since migration 0002. Writing a job state into it was caught by
  that CHECK constraint; the state lives in `meta_json` instead.
