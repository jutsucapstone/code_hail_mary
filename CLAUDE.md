@AGENTS.md

# JUTSU — Corporate Memory Graph

Enterprise Memory OS: a temporal knowledge graph of an organisation's people, projects,
decisions, meetings and skills, served through six surfaces. Read-only into source systems.
Nothing is written back.

**Full spec: `docs/jutsu-master-spec.md`. Current slice: `docs/plan-phase-1.md`.**
Read the slice before writing any file.

---

## The one invariant

**A fact is only true in JUTSU if it points at the evidence that produced it, and only visible
if the caller can see that evidence.**

If a change weakens provenance or ACL fidelity, it is wrong regardless of how much faster or
cleaner it is. When unsure, take the option that keeps that sentence true.

---

## Non-negotiables

Hard gates. Violating one is a defect regardless of whether tests pass.

**Provenance**

1. Every LLM-derived node and edge carries `evidence[]` — `{chunk_id, char_start, char_end,
   quote, extractor_version, prompt_hash, model, confidence}`. Not nullable, not deferred.
2. **Hallucination gate:** an extraction claim's `quote` must appear verbatim in the source
   chunk, or the claim is discarded.
3. Answers are assembled from retrieved evidence, never model memory. Uncited assertions →
   retry once → `insufficient_evidence`. A fluent guess is a defect, not a near-miss.
4. Extraction is versioned. Re-running supersedes; it never overwrites.

**Security**

5. ACL filtering happens **inside the SQL query**, never as a Python post-filter.
6. A graph fact whose entire evidence set is ACL-invisible to the caller is invisible to them —
   filtered before it reaches the LLM.
7. `org_id` on every row, every node, every Cypher template. No exceptions.
8. All connectors are read-only. No write scope in any OAuth flow, ever.
9. No PII in logs, traces or error messages. Structured JSON with `trace_id`, `org_id`, opaque
   `user_id`.
10. Secrets from Secret Manager only. `.env.example` committed, `.env` never.

**Engineering**

11. No mock data behind any UI surface. Unfinished work is feature-flagged off, never faked.
12. Every schema change is a migration — Alembic for Postgres, numbered Cypher for Neo4j.
13. Typed end to end: Pydantic v2 → OpenAPI → generated TypeScript client. Frontend types are
    never hand-written.
14. Ingestion is idempotent on `(org_id, source_system, external_id, content_hash)`.
15. `make preflight` passes before any commit. A hook enforces this.

**Responsible AI**

16. Risk scores measure **knowledge concentration**, never an individual's probability of
    resigning.
17. Any surface that ranks people must expose *why* to the person being ranked.
18. Individual-level risk drill-down requires explicit consent on the Person node. Default
    views are aggregate.

---

## Stack — fixed, do not substitute

```
Frontend   Next.js 16 App Router · TypeScript · Tailwind v4 · shadcn/ui · TanStack Query
Gateway    Python 3.12 · FastAPI · Pydantic v2 · SSE streaming
Agents     LangGraph (Postgres checkpointer) · LangChain for document loaders only
Graph      Neo4j 5 — AuraDB prod, CE container dev
Vector     PostgreSQL 16 + pgvector HNSW
LLM        Gemini via Vertex AI. Flash by default; Pro for extraction only.
Queue      Cloud Tasks (prod) / arq + Redis (dev), behind one interface
IaC        Terraform · CI GitHub Actions → Cloud Run
```

**Deliberately absent:** Kafka, Kubernetes, service mesh, Elasticsearch. Four engineers, twelve
weeks. Cloud Run plus a queue is enough.

Python runs through **uv** (`uv run …`) — there is no system `python` on this machine.
Node runs through **pnpm** workspaces. Dev server is port **3210**, not 3000.

---

## Working agreement

1. **Read the phase spec first** — `docs/plan-phase-N.md`. Read the whole slice before writing.
2. **One slice per session.** Start slice N+1 in a fresh session so the spec is re-read rather
   than half-remembered.
3. **Plan mode before code.** Propose files and interfaces; wait for approval; then build.
4. **Vertical, never horizontal.** One document ingested → visible in the graph → answerable,
   beats three weeks of scaffolding.
5. **Tests with the code.** Coverage ≥70% on core, graph, retrieval.
6. **ADR every real decision** in `docs/adr/`. If you explain a choice twice in chat, it should
   have been an ADR.
7. **No TODOs in merged code.** Implement it or open an issue and reference the number.
8. **Never invent numbers.** Metrics come from `make eval`, latency from traces, cost from token
   accounting. If you do not have the measurement, say so.
9. **Surface uncertainty immediately.** Silent scope reinterpretation is the most expensive
   thing you can do here.

---

## Known traps

- **Chunk offsets are against the original text, not the masked text.** `to_original()` is the
  only correct translation path. An off-by-one here is a visible product bug — it mis-highlights
  the citation span.
- **PII pseudonyms are scoped to a document on purpose.** `[EMAIL_A7]` is derived from
  the document as well as the value, so the same address is a different token in the next
  document. Making them global would make cross-document entity resolution easy and would
  turn any masked-text export into a correlation table for anyone who never passed an ACL
  check (ADR 0005). Cross-document identity goes through the vault, under the check.
- **"Masked" does not currently mean "no names."** There is no PERSON or ADDRESS detector
  — that needs NER, which is a stack decision. Addresses, phones, SSNs, cards and IBANs
  are masked; names are not. Say so in the DPIA, not only in the ADR.
- **Only a hard split can land anywhere, so only a hard split needs the Unicode care.**
  Every other chunk boundary follows whitespace or a line start. The token-limit cut does
  not, and it will slice a Devanagari virama or a ZWJ emoji off its base unless it is
  moved back off the cluster. Offsets stay correct either way, so nothing fails - the
  model just reads a fragment of a character (ADR 0006).
- **HNSW plus a restrictive ACL filter returns fewer than `k`.** Raise `ef_search` and
  over-fetch. Never "fix" a short result set by loosening the filter.
- **Random-sampling Enron destroys the reply graph.** Sample complete threads or entity
  resolution has nothing to resolve — and you will not notice until week 5.
- **Entity merges must be reversible.** Write `ALIAS_OF`; never destructively merge two Person
  nodes. A wrong irreversible merge silently corrupts every downstream score.
- **LLM-generated Cypher runs read-only**, with a statement timeout and a label allowlist.
- **Query and document embeddings use different `task_type` values.** Using one for both costs
  several points of recall and is invisible until eval.

### Embedding traps (`packages/retrieval`)

- **`gemini-embedding-001` at 768 dimensions returns an UNNORMALISED vector.** The 3072
  default is unit length; the MRL-truncated 768 is about 0.58. Cosine is scale-invariant
  so `vector_cosine_ops` still ranks correctly, but never assume unit length, and
  normalise before storing (ADR 0009).
- **Over-long input is truncated SILENTLY, under HTTP 200.** The response carries
  `truncated=true` and a well-formed vector describing only a prefix. Nothing in the
  status says the answer is wrong. Never persist one.
- **Requests per minute is the constraint, not instances per request.** 250 instances in
  one request works; eight rapid requests trips the quota. Batch large, concurrency low.
- **Retrying a 400 is not harmless.** It is rejected identically every time, and on a
  corpus-sized job it spends real quota to be told so repeatedly.
- **`estimate_tokens` under-counts masked text** (0.75x on `[EMAIL_A7]` pseudonyms). Safe
  only because 768 sits far below the 2048 input limit. Do not raise `target_tokens`
  toward the limit on the assumption that it over-counts. It also must never reach
  `TokenLedger.charge` — the estimate picks batch boundaries, the provider's
  `token_count` is what is billed and therefore what the ceiling counts.
- **`TOKEN_BUDGET_PER_REQUEST` is NOT the embedding ceiling.** It is §13's per-request
  bound; today `apps/api/.../retrieval.py` reads it into a fresh `TokenLedger` for each
  `/v1/search`, `/v1/ask` and KT copilot question, and exceeding it is a 429. The
  embedding job ceiling is `EMBEDDING_TOKEN_BUDGET`. Wiring the former into
  `EmbeddingSettings` looks like a fix and would stop a 45k-document seed after a few
  dozen documents.
- **A ceiling checked only after a batch returns still pays for the queue behind it.**
  `charge` detects an overrun; `TokenLedger.check()` before each request prevents the
  next one. And `asyncio.gather` propagates the first exception *without cancelling its
  siblings*, so those batches called the provider after the budget was gone — the tasks
  are now cancelled explicitly. Both halves are pinned by tests that assert
  `FakeTransport.calls`, because raising correctly while still spending looks identical
  from the caller.
- **`EMBEDDING_TOKEN_BUDGET=0` is refused, not read as unlimited.** Unset means no
  ceiling; zero is a mistyped ceiling, and silently treating it as infinite is how a
  guardrail disappears without anybody removing it.
- **The model is `gemini-embedding-001`, and §9.3 of the spec disagrees.** ADR 0009
  supersedes it on measurements; spec amendment **A3** records that so the two documents
  no longer conflict. `text-embedding-004` is the documented fallback. Every measured
  constant here — the 0.58 norm, the 2048 truncation boundary, the 250-instance batch,
  the recorded fixture — describes `gemini-embedding-001` in `asia-south1` and describes
  nothing at all on another model.
- **Running Alembic in-process disables the application's loggers.** `fileConfig` defaults
  to `disable_existing_loggers=True`; `env.py` now passes `False`. An audit line that is
  never emitted is indistinguishable from an action that never happened.


### Retrieval traps (`packages/retrieval/search.py`)

- **`search_chunks` has no `principals` and no `org_id` parameter, and that is the design.**
  Principals are resolved inside it from `user_id`; the tenant comes from the RLS GUC.
  Adding either parameter would let a call site widen an authorization decision, and would
  reintroduce the stale-set bug that makes revocation take effect "eventually" (ADR 0011).
- **`ACL_PREDICATE` is a constant because the escalation ladder must provably re-run the
  same predicate.** Build it per call and the one bug that matters — a wider filter on the
  retry — becomes both possible and invisible in review.
- **`hnsw.ef_search` does not exist until pgvector's library loads into the backend.**
  `SHOW hnsw.ef_search` on a fresh connection raises `UndefinedObjectError`. `set_config`
  works — Postgres accepts it as a placeholder and pgvector validates it on load — so set
  it, never read it back to check.
- **A short result set costs one extra query, deliberately.** Escalation stops when a wider
  `ef_search` finds nothing new, and that probe is the only thing distinguishing "you are
  not authorized to see more" from "the index did not look hard enough".
- **`principal_type = 'public'` is silently not honoured.** The check constraint permits it,
  §12's filter does not mention it, so such a document is invisible to everyone. Fail-closed
  and pinned by a test — but if a connector ever emits it, those documents vanish with no
  error. Giving it a meaning is an ADR.
- **The `EXPLAIN` test is the only one that asserts the mechanism.** Every other retrieval
  test asserts an outcome, and a Python post-filter produces identical outcomes until a
  count, a `LIMIT` or a cursor is involved.
- **Two things silently cost 100x, and neither fails a test.** Measured at 40k chunks with
  an org-wide grant: a `JOIN documents` inside the vector scan makes the planner drive from
  `documents` and never open the index (3016ms); a `, c.id` tie-break in the inner
  `ORDER BY` forces a full sort and abandons it again (203ms). `chunks` alone, ordered by
  distance alone, is 15ms. The joins and the tie-break belong in the outer projection over
  the `k` authorized rows. Two string assertions pin this, because the entire symptom is
  slowness.
- **`ef_search` is inert whenever the planner picks the ACL-first plan.** A restrictive ACL
  makes the authorized set small enough to rank exactly — which is correct and has no recall
  loss — but it means the ladder is *not* what makes restrictive queries fast. Do not read a
  fast restrictive query as evidence the ladder works.
- **A permission must never be named after a data-plane object.** `evidence:read` was
  refused by `test_no_permission_grants_document_visibility` and became `retrieval:query`.
  Such a name reads as a grant over that object whatever the docstring says, and the next
  person to add one follows the precedent.

### Corpus traps (`packages/connectors`)

- **`make seed` on the real corpus needs `--sample`.** Without it the walk is a directory
  walk that takes whatever it meets up to `--max-documents`, which on a 500k-message
  maildir is the random sampling §19 forbids. `--sample` runs `sample_enron`, writes
  `sample_manifest.json` beside the corpus and records it on the source row; the source
  then ingests those identifiers and nothing else, with the whole-corpus thread ids.
- **Manifest version 2 carries `message_threads`, and version 1 is refused.** Without the
  map an ingest driven from a manifest falls back to `LocalConnector.fetch`, whose own
  docstring calls its thread "best-effort" and "wrong for a corpus" — so it would sample
  complete threads and then reassemble them incorrectly. A stale manifest is refused
  rather than upgraded, and every manifest failure is `UnsupportedSource` (permanent),
  because retrying a bad file re-reads the same bad file.
- **Never commit mail fixtures as files.** `.gitattributes` is `* text=auto eol=lf` and a
  MIME boundary is defined in terms of CRLF, so git rewrites the fixture into something
  that parses differently from the mail it imitates. Build them from Python with explicit
  CRLF (ADR 0008).
- **`os.walk(followlinks=False)` does not protect against a symlinked file.** It governs
  directory recursion only. Re-check containment after resolving every path.
- **A `Date` with no timezone is unknown, not UTC.** Assuming UTC moves a message by up to
  twelve hours and thread ordering reads that field.
- **ACL principals in the corpus are email addresses, not IdP subjects.** `users.external_id`
  holds a subject for a real tenant, so these match nothing the day one connects — and a
  filter that matches nothing looks exactly like a correct filter returning nothing.

### Neo4j traps (`packages/graph`)

- **Neo4j has no row-level security, so a forgotten filter returns every tenant.** The
  Postgres reflex is wrong here: there `WHERE org_id` is a belt over a policy that already
  fails closed, here it is the only thing there is. `GraphSession.run` refuses any Cypher
  without `$org_id`, and `ddl_session` is the single deliberate exception (ADR 0007).
- **`default_access_mode=READ_ACCESS` does not block writes on a single instance.** It
  routes to a follower on a cluster; on the dev container it is inert. The static
  write-clause check is what actually holds locally, which is why both exist.
- **Cypher cannot parameterise a label or relationship type.** Everything else is a bound
  parameter; those two come from `labels.py` or they do not go in at all.
- **`upgrade` verifies migration checksums and `downgrade` deliberately does not.** That
  asymmetry is the recovery path: a ledger the runner refuses to read is fixed with
  `make migrate-graph-down` then `make migrate-graph`.
- **Property existence constraints are Enterprise.** Nothing at the database level
  requires `org_id` to be present on a node. A write that omits it creates a node no
  scoped query can ever see.

### Identity and ACL traps

- **Email is not an authorization identity.** `users.email` is display and compatibility
  data. The ACL principal is a namespaced provider subject in `source_identities`
  (ADR 0010). An email change must never move authorization.
- **`users.external_id` is NOT the ACL principal any more.** It kept its column and lost
  its meaning in migration 0008. Nothing in the authorization path reads it; one column
  could not hold a Google `sub`, an Entra `oid`, a Slack member id and an Atlassian
  `accountId` at the same time.
- **Principals are namespaced `{source_system}:{subject}`.** Without the prefix a Slack
  member id and a GitHub numeric id share a string space, and a grant from one system
  could authorize a principal from another.
- **Resolution is eager and must never be cached.** §17 test 3 requires a group removal to
  take effect on the next query with no flush; the same holds for a revoked identity. A
  cache that outlives a revocation is an authorization decision made from stale state.
- **The organisation never comes from a call site.** `scoped_acl_principals` takes no
  `org_id`; the scope is the GUC set from the session. A parameter there would be the
  regression.
- **A new authorization input needs `org_id` and RLS on the day it lands.** `user_groups`
  shipped with neither and fed the §12 filter for six migrations before anyone noticed.
- **Linking a source identity IS granting document access.** Not an admin convenience — a
  row in `source_identities` is the difference between a caller seeing a document and not
  seeing it. Everything in `apps/api/src/jutsu_api/identities.py` follows from that.
- **An administrator may never link a subject to themselves, and it is not a permission
  check.** §17 keeps roles and ACLs apart: nothing in `Permission` may confer a document
  read. Gate it on a permission and it stops being a refusal for an Owner, who holds every
  permission there is. Self-*revocation* is allowed — removing your own access is not an
  escalation.
- **An automatic link takes the address from the verification, never from the request.**
  Registration passes the value it wrote to `users.email`; invitation acceptance passes the
  address the token reached. `/v1/invitations/accept` carries a free-text `full_name`, and
  the day that string reaches a subject, accepting an invitation becomes a way to claim any
  principal in the tenant.
- **`ON CONFLICT DO NOTHING` on an automatic link is fail-closed, not a no-op.** If the
  address is already held by someone else in that tenant, the new user gets **no** local
  principal. Correct: one subject is one person per tenant, and nobody's access moves.
- **`revoke_all_for_user` has no production caller.** The offboarding route does not exist
  yet. When it lands it must call that primitive rather than looping — the audit rows are
  written inside it for exactly that reason.
- **The OAuth callback auto-links its proven subject (ADR 0014), and that is the
  verification precedent, not a hole in the self-link refusal.** The refusal exists
  because an admin *asserts*; the callback's subject came from the provider's identity
  endpoint under a token minted seconds earlier. Fail-closed on conflict like the email
  path. Disconnecting a connection deliberately does NOT revoke the identity — a pipe is
  not a person; identity revocation stays the explicit admin act.
- **Provider connectors grant `owner_acl` and nothing wider — floor AND ceiling.**
  Providers report sharing as email addresses, and emails are not subjects; a wider grant
  minted from them would be a guess wearing an ACL. `owner_acl` in
  `jutsu_connectors/providers/base.py` is the only grant-minting path, on purpose.

### Live-connection traps (`jutsu_core.providers`, `jutsu_worker.credentials`, `fetchers`)

- **Refreshed tokens commit IMMEDIATELY, never with the sync's transaction.** Atlassian
  rotates refresh tokens: the moment the provider answers, the old one is burned at
  their end. Tie the write to the sync and a transient failure rolls back to a token
  the provider will never honour again — a permanent reauth loop built out of tidiness.
  `ConnectionTokenSource` owns this; `access_token_for` leaves it to the caller.
- **Slack scopes ride `user_scope`, never `scope`.** Slack's `scope` parameter
  provisions a *bot*; the employee's own visibility is a user token read out of
  `authed_user` on the exchange. And Slack answers HTTP 200 with `ok:false` — every
  Slack response goes through an unwrap that refuses it.
- **GitHub's `repo:status` (and `repo`) write.** §4.8 admits no write scope, so GitHub
  is bounded to what `read:user`/`read:org` reach: public repositories. Private-repo
  read-only is a GitHub App installation — a different flow, not a bigger scope. A
  registry-wide test pins every provider against a known-write-scope list.
- **`sources.system` is the ACL namespace, not the provider id.** All four Google
  products land as `gmail`, the three Microsoft ones as `m365` — the enum is the
  constraint and the subject is shared. The precise provider lives in
  `config_json.provider`, which is what `CONNECTOR_CLASSES` keys off.
- **The doorbell answers the ADR 0012 sweeper gap without a bypass.** The API cannot
  enumerate orgs and neither may the worker (`orgs` is RLS-forced; a BYPASSRLS helper
  is the service-role bypass the rules forbid). Instead the API publishes
  `drain_org_jobs` for the org its session already holds, deferred 2s past commit,
  best-effort by contract; one doorbell drains that org's whole backlog, so a lost
  message costs latency, never work. An org nobody ever rings for still keeps its
  orphaned jobs — that residue of ADR 0012 stands.
- **The anthropic SDK type-hints against `httpx2`** (its vendored fork). Constructing
  its exceptions in tests with plain `httpx` objects fails mypy only — import
  `httpx2 as httpx` in that test module.


### Ingestion traps (`apps/worker`)

- **A `SECURITY DEFINER` function over a FORCE-RLS table returns zero rows, with no error.**
  Verified, not assumed - and it is why there is no cross-tenant job sweeper. Recovery is
  org-scoped and runs at the start of each source run, so **an organisation whose source is
  never processed again keeps its orphaned jobs** (ADR 0012). The doorbell narrows this:
  the API publishes an org-scoped `drain_org_jobs` message whenever it enqueues work, and
  one message drains that org's whole backlog — but an org nobody rings for still keeps
  its orphans, and that residue is deliberate (no cross-tenant enumeration, no bypass).
- **The claim must commit before the work starts.** Claim and work in one transaction and a
  failure rolls back the `attempts + 1` as well, so the job looks untouched and retries for
  ever. Bounded attempts are only bounded if the counting survives the failure.
- **Record a failure in a NEW transaction.** Postgres aborts a transaction at the first
  error, so a handler writing `failure_kind` into the failed transaction records nothing and
  raises something unrelated.
- **A completed job shadows its identifier for ever unless the walk reopens it.** The
  document key is the identity, not the version, so without `reopen_completed_job` a changed
  document is never ingested again. `failed` and `dead_letter` are deliberately NOT reopened
  - that would be an infinite loop nobody sees.
- **Superseding needs the FK deferred, and the ORDER of the two statements is fixed.** Insert
  first and the partial index sees two current versions; supersede first with an immediate
  FK and it references a row that does not exist. `SET CONSTRAINTS
  fk_documents_superseded_by DEFERRED`, then update, then insert.
- **`migrate-pg-down` refuses once anything has been superseded.** That is the guard working:
  the old non-partial constraint cannot represent version history. Clear the data or stay on
  0010; do not "fix" the migration to delete versions.
- **An expired lease does NOT make a running job reclaimable, and that is load-bearing.**
  The arithmetic looks broken - five attempts each able to obey a 120s `Retry-After`
  against `DEFAULT_LEASE_SECONDS = 300` - but `run_embedding_job` writes the job row
  before the provider call, so the work transaction holds a row lock for the whole job.
  A reclaiming `UPDATE` blocks on it and `claim_job` skips the row. Measured, not
  assumed. It means the "improvement" of committing the state transition early, or
  moving it after the slow call, turns crash recovery into duplicate provider spend.
- **`--max-documents` no longer bounds the embedding drain; `--max-embed-jobs` does.**
  The old coupling worked only because every fixture document was exactly one embedding
  job. Omitting the new flag keeps the old behaviour exactly.
- **The cursor is taken BEFORE the walk.** Taken after, a file modified mid-walk falls
  between the two instants and is never seen again. Taken before, it is re-listed next run
  and costs one `unchanged` outcome.
- **`audit_log.outcome` is `success | denied | failure` and nothing else.** A job state
  written there fails the CHECK constraint from migration 0002; states go in `meta_json`.
- **`org_session` caches one engine per process.** A test that changes `DATABASE_URL` must
  call `dispose_engine()`, or it runs against a pool bound to a schema that no longer exists
  - the suite then passes one test at a time and fails as a file.

### Eval and gate traps (`packages/evals`)

- **A gate has three outcomes, and the third one is load-bearing.** `not_measured` is not
  a failure and not a pass. `CheckResult` *refuses* to hold an `observed` or a `threshold`
  alongside it, because the way rule 8 actually gets broken is a check that could not run
  reporting the value of a variable nobody assigned — `count(*)` over a database nobody
  named returns 0, and `0 >= 45_000` is a well-formed failure that means nothing (ADR 0013).
- **A skipped test is unmeasured, never green and never red.** M1 says "all 7 ACL tests
  pass"; a suite that skipped exits 0 having proven nothing. Calling that a failure blames
  the code for the harness's circumstances, and calling it a pass is the exact defect the
  root `conftest.py` reachability probe was written to fix.
- **Coverage is the one clause where a skip moves the number rather than leaving a gap.**
  With the containers stopped, `jutsu_graph` measured 59.3% and the clause went red — a
  confident figure about a run where 394 tests never executed. Any skip during the coverage
  run makes `coverage_core` unmeasured.
- **Select a test by `-k`, not by node id.** A node id has to name the class, so
  `TestReversibility` becomes part of the gate's contract with a file it does not own, and
  moving the test turns the clause into "collected no tests" — unmeasured, and silent.
- **`make` is `mingw32-make` here.** There is no `make` on the Windows dev machine, so
  `shutil.which("make")` alone reported the preflight clause unmeasured on the machine the
  slice was written on. `_MAKE_NAMES` tries three.
- **The offset check must not replay the chunker.** Re-running `chunk_document` proves
  determinism, and a chunker with an off-by-one reproduces it perfectly. `remask_slice`
  uses only the two stored numbers, the mask spans and string slicing.
- **`evals/reports/` is committed; `evals/runs/` is not.** A report holds check names,
  outcomes, scalars and a commit — §18 wants a metric to name a commit. A receipt describes
  a local corpus, so it stores a *hash* of the corpus root: a resolved path on a developer
  machine contains their account name, and §4.9 has no carve-out for files that are not
  called logs.
- **`seed_idempotent` is the only check that writes, and it must keep asking.** "A second
  `make seed` adds zero rows" cannot be observed without a second `make seed`. It takes the
  ingestion entry point as an injected callable — `jutsu_evals` is a package and must never
  import `apps/worker`.
- **S9 shipping is not an M1 pass.** The harness exists; nine clauses on this machine are
  unmeasured for want of services and a corpus. The summary line will not print
  "gate PASSED" over any run with an unmeasured clause.


### Scheduled sync and the clock (`sched` schema, `jutsu_worker.schedule`)

- **No role in this deployment can enumerate organisations, and that is the whole design
  problem.** `orgs` is `ENABLE` + `FORCE` RLS; `jutsu_app` sees zero rows without the GUC;
  and production's migration role is `postgres` with `rolsuper = false` and
  `rolbypassrls = false`, so it sees zero too — measured against production, not assumed.
  Dev disagrees, because the Compose bootstrap role IS a superuser, so a migration that
  reads `orgs` backfills twelve rows locally and nothing at all in production. Anything a
  clock needs to know about tenants comes from an org-less index (`sched.org_sync_schedules`,
  seeded from `auth.identity_memberships`), never from `orgs` (ADR 0018).
- **`sched` is contained by privilege, not by RLS, because it holds no tenant content.**
  `jutsu_app` has `USAGE` on the schema and `EXECUTE` on five functions and *no table
  privilege at all* — a direct `SELECT` from a request path is "permission denied", which
  a test asserts. `read_schedule()` and `write_schedule()` take no `org_id`: they read
  `app.current_org_id`, so no call site can name a tenant. Adding a parameter there is the
  regression, exactly as it would be in `scoped_acl_principals`.
- **`due_organisations()` is the one deliberate cross-tenant read in the system.** Three
  columns, no content, one caller. Same trust shape as `auth.reap_expired_registrations()`.
  If a second caller appears, that is an ADR, not a convenience.
- **A schedule is stored as an IANA name and resolved with `AT TIME ZONE`, never as an
  offset.** An offset is wrong for half the year in any zone that observes daylight saving,
  and "01:00" has to keep meaning 01:00. Test due-ness against a real zone (`Asia/Kolkata`
  is +05:30, so 01:30 IST is 20:00 UTC the day before): a schedule tested only at UTC passes
  with the timezone ignored entirely.
- **`zoneinfo` reads the operating system's tz database, and Windows has none — nor does a
  slim container.** `ZoneInfo("Asia/Kolkata")` raises `ZoneInfoNotFoundError` on this dev
  machine. `tzdata` is therefore an explicit dependency of `apps/api`; removing it as
  "unused" breaks every schedule read at runtime and nothing at import time.
- **The nightly job shares `sync_now`'s idempotency key**, `connector.sync:{org}:{connection}`.
  That is what stops a nightly run and a person pressing "Sync now" a second earlier from
  producing two walks of one connection. A fourth enqueue site must use it too.

### Provider connector traps found in the production pass

- **A bounded listing loop must raise, not return.** Every connector pages inside
  `for _ in range(_MAX_PAGES)`, and falling out of that loop used to be indistinguishable
  from the provider having no more pages. `run_source_walk` then advanced
  `last_sync_cursor` to the moment the walk *started*, so every document past the bound
  was filtered out of the next listing by that very cursor and never seen again — a first
  sync of a large mailbox indexed the newest few thousand messages, reported success, and
  lost the rest. The loops now raise `ListingIncomplete`; the walk keeps what it
  enqueued, leaves the cursor alone, and records `listing_complete: false`. **A walk that
  hits the bound therefore re-lists the same window next time and does not advance** —
  honest, but not yet resumable. Resuming a truncated first sync needs a backfill cursor
  (a second, downward bound); it is not built, and that is the residue.
- **The proven subject is a per-provider field, never a ladder.** `sub or account_id or
  user_id or id` reads Zoom's `GET /v2/users/me` — which carries the employee's `id` and
  the whole account's `account_id` — and picks the account. Every colleague then minted
  the same `zoom:<account_id>` ACL principal: the first to connect received everyone's
  recordings and the rest received none, because linking is fail-closed on conflict.
  `Provider.subject_fields` declares it; the same key is correct for Atlassian, where
  `account_id` IS the person, which is exactly why a shared ladder cannot work.
- **`classify()` must name the connectors' own errors.** `ProviderAuthError` and
  `ProviderApiError` used to fall through to `INTERNAL, retryable`, so a grant the
  employee revoked at Google was recorded as an internal bug, retried five times, and
  never flipped the connection to "reconnect". `mark_reauth_required` fires on
  `ProviderAuthError` as well as `ReauthRequired` — the first is the provider refusing a
  token, the second is the credential layer failing to mint one.
- **`GET /rest/api/3/search` no longer exists.** Atlassian removed the offset-paged Jira
  search along with `startAt` and `total`; `/rest/api/3/search/jql` is token-paged and
  returns `isLast`/`nextPageToken` and no count. A connector still calling the old path
  gets a permanent rejection on its first call and lists nothing, for ever.
- **A document that is not there is not a failure.** Listing and fetching are two calls,
  and GitHub forces the case: `/user/repos` says nothing about whether a repository has a
  README, so most `readme:` identifiers 404 on fetch. `DocumentGone` -> `IngestOutcome.ABSENT`
  completes the job having written nothing, instead of a permanently failed row per
  README-less repository sitting in the administrator's Jobs view.
- **Confluence's `_links.next` carries a cursor, and recomputing `start` is not the
  same.** Deep offsets over a CQL search are documented as unstable, so a walk counting
  its own way through can repeat a page or step over one — and a repeat is deduplicated
  by content hash while a skip is simply absent. Only the link's *query* is replayed; the
  path is rebuilt from the resolved cloud id, so a link pointing elsewhere cannot
  redirect an authenticated call.
- **Zoom's listing is filtered by whole DATES and a recording appears only once Zoom has
  processed it.** With the window floored to the cursor's own date, a meeting whose
  recording finished processing after the next walk fell in the seam and was never
  listed. `_LATE_ARRIVAL_DAYS` re-opens the window a little; the cost is re-listing
  identifiers whose idempotency keys already exist.

### Logging and transport traps

- **Cloud Logging reads `severity` and `message`, and nothing else is promoted.** The
  formatter emitted `level` and `msg`, so every line — including a drain's traceback —
  was ingested at DEFAULT: `severity>=ERROR` matched nothing and no alert built on it
  could fire. Both spellings are emitted now; do not "tidy away" the duplicate.
- **A Subject header may not contain a line break, and `MimeMessage` raises rather than
  folding one.** Four subjects interpolate a customer-typed name and nothing on the way
  in rejects a newline, so `"Acme\nCorp"` raised *inside the transport* — a 500 with no
  mail sent, invisible in development because `ConsoleEmailSender` builds no MIME message
  at all. `_render` collapses whitespace with `str.split()`, which covers U+0085, U+2028
  and U+2029 that a `[\r\n]` filter would pass. `subject_name()` additionally bounds
  what free text from the *public* registration endpoint can put in a subject.
- **A `__Host-` cookie without `Secure` is rejected outright, deletions included.**
  `delete_cookie(..., path="/")` takes Starlette's `secure=False`, so the browser threw
  the deletion away and the cookie lived out its full lifetime. Pass
  `secure=settings.cookies_secure` on every `__Host-` cookie, including when clearing it.
  No test in this suite can see it: httpx's cookie jar implements no prefix rule.

### The clock's own traps

- **A lease has to be written in both places.** `sched.mark_started` has always carried
  the takeover branch for an unfinished claim older than an hour, but
  `sched.due_organisations` is the only thing that ever offers an organisation to it —
  and it excluded anything started today, finished or not. The branch was unreachable
  from its only caller, so a run killed mid-flight cost the whole local day in silence.
  The two predicates must say the same thing.
- **The schedule's default is 01:00 `Asia/Kolkata`, and it is a column default.** `UTC`
  put an untouched tenant's first sync at 06:30 local — inside the working day, against
  the accounts its people were using. `sync_schedule.DEFAULT_TIMEZONE` must match
  `sched.org_sync_schedules.timezone`'s default; a test pins the two together.
- **`sync:schedule_manage`, not `org:update`.** The clock's owners are Owner, Super
  Admin, IT Admin and HR Admin; `org:update` does not reach HR, and widening it would
  have granted HR the organisation's profile and its connection policies too. Gate the
  form on the same permission the route requires, or an HR Admin reads a schedule they
  may set and is given no control.

### Invitation and bulk-onboarding traps (`jutsu_api.invitations`, `bulk_invitations`)

- **`uq_invitations_org_email_live` cannot mention `expires_at`, and that is not a
  mistake anyone can fix.** `now()` is not immutable, so no index predicate may reference
  it; the partial index is `accepted_at IS NULL AND revoked_at IS NULL` and its own
  migration comment claims expired invitations may be reissued. They could not be — the
  expired row still occupied the slot, the INSERT raised `IntegrityError`, and the
  refusal read "that person already has an invitation waiting", which was the one thing
  that was not true. `invite_employee` now revokes the expired row before inserting. Do
  not "simplify" that UPDATE away because the index looks like it already covers it.
- **A bulk row needs a savepoint, and only a DATABASE error proves it.** Postgres aborts
  the transaction at its first error, so one failing row would otherwise discard every
  invitation after it while the batch reported success. The regression test induces
  `SELECT 1 / 0` rather than raising in Python on purpose: a Python `raise` leaves the
  connection perfectly healthy, so a test built on one passes with `begin_nested`
  removed.
- **Delivery happens after the savepoints, so a failed send REVOKES rather than rolls
  back.** Its savepoint is long released by then. Revoking reaches the same end state and
  — because the unique index is partial on exactly `revoked_at IS NULL` — is also what
  lets the administrator retry that address. Deleting the row instead would destroy the
  record that JUTSU tried.
- **The preview is advice; the send re-decides.** Rows come back from the browser, so
  `invite_many` re-runs `classify` and `invite_employee` re-runs `outranks`. Trusting the
  preview would make the rank ceiling a client-side check.
- **`openpyxl` is imported inside `parse_xlsx`, not at module scope.** It drags in its own
  XML machinery, and every API process would pay for it at startup to serve a route most
  of them never see.
- **`navigator.clipboard?.writeText(v)` reports success when there is no clipboard.**
  Optional chaining makes the whole expression `undefined`, which `await` resolves — so
  the obvious spelling says "Copied" on exactly the insecure origins that copied nothing.
  `components/copy-button.tsx` tests for the object explicitly. The call sites it replaced
  used `void navigator.clipboard.writeText(...)` followed by an unconditional success
  toast, which was the same lie with an extra step.
- **jsdom implements neither `Blob.text()` nor `Blob.arrayBuffer()`.** Both are Baseline in
  every browser this targets and are how the onboarding panel reads a dropped file. The
  polyfill lives in `apps/web/vitest.setup.ts` — reaching for `FileReader` in application
  code to satisfy a test environment would put a 2010 API into the codebase for reasons no
  reader could infer.

### Postgres driver traps (asyncpg, via SQLAlchemy)

- **One statement per `op.execute`.** asyncpg prepares every statement, and a prepared
  statement cannot carry several commands — a migration with a multi-line `GRANT …; GRANT …;`
  block fails with "cannot insert multiple commands into a prepared statement". Loop over a
  tuple of statements instead.
- **A parameter bound to two columns of different types cannot be deduced.** Reusing `:org`
  for both `audit_log.org_id` (uuid) and `audit_log.resource_id` (text) raises
  "inconsistent types deduced for parameter $1". Bind the same value twice under two names.
- **A `timestamptz` parameter must be a `datetime`, not a string with a `CAST`.** asyncpg
  encodes by Python type before Postgres ever sees the cast.

### Postgres / RLS traps (`packages/db`)

- **A superuser bypasses RLS unconditionally, and `FORCE` does not change that** — FORCE only
  covers the table *owner*. The app therefore connects as `jutsu_app`
  (`NOSUPERUSER NOBYPASSRLS`); migrations run as the owner via `MIGRATION_DATABASE_URL`.
  Point the app at the owner and every policy goes silently inert while every isolation
  test still passes. `test_app_role_cannot_bypass_rls` exists to catch that regression.
- **`current_setting('app.current_org_id', true)` returns NULL only until the GUC is first
  set.** Afterwards a fresh transaction reads `''`, and `''::uuid` *raises* instead of
  filtering. Every policy predicate wraps it in `NULLIF(…, '')` so unset and reset both
  fail closed.
- **`SET LOCAL x = :param` is a syntax error** — `SET` is a utility statement and takes no
  bind parameters. Use `set_config(name, value, true)`, which is transaction-scoped *and*
  parameterisable, so an org id from a request context is never concatenated into SQL.
- **Chunk and ACL rows carry a denormalised `org_id`** with a composite FK to
  `(documents.id, documents.org_id)`. Dropping it to "match §8" makes the RLS policy a
  correlated subquery on the hot retrieval path (ADR 0002).

### Landing-page traps (`apps/web`)

- `--brand` / `--graph` **flip lightness role between themes**; `--brand-foreground` inverts to
  match. Re-check contrast in **both** themes after any palette edit.
- The JUTSU wordmark is **vector-traced from the supplied artwork** (`lib/wordmark-paths.ts`),
  never a font. Regenerate with `node scripts/trace-wordmark.js`.
- FAQ and architecture panels **stay mounted when collapsed** (`inert` / `hidden`). Unmounting
  breaks `aria-controls` and hides answers from crawlers.
- Full-res logo lives in `assets/` and is **not served**. `public/jutsu-logo.png` is a generated
  256px copy — putting the 1254px original back costs ~900KB of deploy weight.

### Console traps (`apps/web/components/console`)

- **A paused query never becomes an error.** TanStack's default `networkMode: "online"`
  refuses to start a fetch when the browser reports offline, and the query sits at
  `status: "pending"`, `fetchStatus: "paused"` for ever — so no surface can render a failure
  and the console shows a loading skeleton with no message and no way out. `lib/query.ts`
  sets `"always"`: every request goes to a *same-origin* proxy, so `navigator.onLine` was
  never evidence about whether it could succeed.
- **Retries are gated on document focus in every network mode.** `retryer.ts` checks
  `focusManager.isFocused()` before continuing one, so a failure in a background or
  automated tab parks until the tab is looked at again. Driving the console from an
  unfocused browser shows a permanent skeleton that has nothing to do with the code. Focus
  the tab before concluding anything.
- **Only a 401 may redirect to sign-in.** Both shells used to bounce on *any* rejection from
  `GET /v1/me`, so a transient 503 sent a valid session to a login page that succeeds, lands
  back, and fails again. A dependency being down is not a reason to doubt who somebody is.
- **Component tests build their own `QueryClient` with retries off**, so they cannot see the
  production defaults at all — the paused-query bug reached a browser because every test had
  overridden the option that caused it. Anything only `createQueryClient` configures belongs
  in `lib/query.test.ts`.
- **`MAIN_CONTENT_ID` belongs on the `<main>` inside the shell**, never on a wrapper in the
  route layout. Wrapping the whole shell put the skip link's target *above* the header, so
  "Skip to main content" skipped nothing — a WCAG 2.4.1 failure that presents as a dead key
  press.
- **Source identities is filed under Access, not Integrations.** Linking one grants document
  visibility; a connector fetches content. The heading is a label like any other, and filing
  it under "Integrations" is what invites somebody to wire a Disconnect button to
  `DELETE .../identities/{id}` and silently revoke a colleague's document access.
- **A static image import is a Next build feature.** `next/image` requires the `width` and
  `height` that its loader attaches; Vite hands the import back as a bare URL string, so any
  test rendering the console header failed on the logo. `vitest.config.mts` supplies the
  shape rather than mocking `next/image` away.

### KT console traps (`apps/api/src/jutsu_api/kt.py`, `kt_workspace.py`)

- **`_open_for` is the KT session.** Binding, expiry and revocation are re-decided on
  every KT request from the cookie principal plus the code; there is no session table
  to invalidate and none should be added. Every recipient-facing function calls it first
  — a route that reads `kt_packages` any other way has re-implemented authorization.
- **Binding before state.** A package bound or addressed to somebody else is a 404
  *whatever its state*. Checking revoked/expired first told the wrong holder the package
  existed and was closed. The right person still gets the exact 403 sentence.
- **The package's subject can never claim it.** An unaddressed package binds to its first
  opener, and the subject — often the person handed the ID to pass on — used to qualify:
  one open bound it for good, a claimed package cannot be re-addressed, and the colleague
  it was for got the uniform 404 for ever. `_open_for` refuses the subject an unbound
  package (reason `subject_of_package`, the same 404). "B cannot open A's package" has two
  other causes the trail tells apart. `unknown_code` is usually B's session sitting in
  another organisation, because `routers/auth.py` opens an identity's OLDEST membership.
  `bound_to_another_user` means somebody else opened it first.
- **`RetrievalWindow` narrows inside the ACL `EXISTS`, and that is the only place a
  narrowing may go.** Two conjuncts on `d.created_at` beside `ACL_PREDICATE`; never a
  `principals`/`org_id` parameter, never a JOIN in the inner scan, never a secondary
  `ORDER BY` key. `test_the_window_sits_inside_the_scan_beside_the_acl_predicate` pins it.
- **History is context, never evidence.** Prior turns reach the model as a labelled
  preamble; the citation gate resolves markers against retrieved passages alone. Numbering
  a history turn like a passage would let an earlier answer launder itself into a source.
- **Stored citations are references.** `kt_messages.citations_json` holds chunk and
  document ids, never passage text; every read re-runs them through `ACL_PREDICATE` and
  marks `available`. The handover summary is still never persisted — the ADR says why one
  and not the other.
- **Two turns, one transaction, one `now()`.** Messages are stamped with
  `clock_timestamp()`, not the column default: `now()` is the transaction's start and is
  identical for both rows, leaving the user/assistant order to a random UUID tie-break.
- **One limiter, keyed by `bucket`.** `spend_budget(Bucket.X, …)` on `search_budget`; a
  new budget is a `Bucket` member and a `_BudgetSpec`, never a second table or an in-process
  counter. Spend before the guarded step, on its own committed session.
- **Coverage has one formula and says so.** `chunks_covered / chunks_total` over the latest
  extraction run of each readable document in the window; `reliable=False` and no number
  when there is nothing to divide. No other percentage exists here, and none may be added
  without a written formula.
- **People are listed by recency, never ranked.** No score, no "who to contact" ordering,
  no per-person figure — non-negotiables 16–18, and no consent field exists in the schema.
- **`auth.touch_session` is called from `resolve_principal`, at most once per
  `SESSION_TOUCH_INTERVAL_SECONDS`.** Before that, every session hard-expired sixty minutes
  after creation. Do not lengthen `SESSION_IDLE_TTL_SECONDS` to "fix" a logout — `config.py`
  explains what idle expiry protects.
- **Inside the KT shell a 403 is the package's refusal, never a role problem.** Every
  recipient route runs `_open_for` first, so the only 403 it can answer is revoked/expired
  with the sentence to show. `components/kt/kt-failure.tsx` renders that; the shared
  `FailureState` would say "your role does not include…", which no role controls here.
- **One observer per KT query, and no `staleTime` on any of them.** Five panels each
  mounting `useQuery` over one key refetched on every mount; the fix was a context from
  `WorkspaceRegion`, not a longer `staleTime` — a revoked package must stop rendering
  evidence-derived labels on the next request, not after a cache window.
- **`uvicorn --reload` on Windows can announce "Reloading..." and keep serving the old
  worker.** The dev API on :8000 answered 405 to the new `PATCH` and 404 to every console
  route hours after the code landed, with the pre-change log format — the replacement
  worker never took the port. A route that "does not exist" on the dev server while
  `emit-openapi.py` lists it means restart the preview, not debug the router.
- **Production runs no Redis and no always-on worker — it runs a Cloud Tasks-rung worker
  service (ADR 0017).** `jutsu_api.queue.ring_doorbell` prefers Cloud Tasks whenever
  `CLOUD_TASKS_QUEUE`, `CLOUD_TASKS_SERVICE_ACCOUNT` and `WORKER_DRAIN_URL` are set; a
  partial set is refused at startup. The worker (`jutsu_worker.http`) is private, scales
  from zero, and re-rings itself; both dispatchers share `jutsu_worker.drain`.
- **A Cloud Tasks name is tombstoned for an hour after it runs.** Task names carry a
  time window (`{bucket}-{org}-{window}`) so a burst coalesces AND the next ring has a
  fresh name. A deterministic name without the window rings exactly once, then is
  refused for an hour with ALREADY_EXISTS — which the code treats as success.
- **A task is scheduled from the END of its window, never from the ring.** With
  `schedule_time = now + delay` and a window longer than the delay, the task for window
  *w* fires while *w* is still open; every later ring in that window is then ALREADY_EXISTS
  against a tombstoned name, reported as success, with no dispatch coming for the row that
  rang. `(w + 1) * window + delay` puts the dispatch after every ring the window can hold.
- **A job left in a WORKING state by a killed worker is counted by neither leftover.**
  It is not claimable (the lease has not lapsed) and not `retry_scheduled`, so a drain that
  reported only those two re-rang for nothing and the row waited for an unrelated doorbell.
  `DrainReport.leases_held` is the third one; it rings just after the earliest lease expires.
- **SQLAlchemy renders bound parameters into its exception text**, and for the documents
  INSERT that is the title, the author's address and 300 characters of the body — reaching
  both `jobs.error` and the traceback uvicorn logs. `create_async_engine(hide_parameters=True)`
  in `jutsu_db.engine` closes both; do not remove it to debug a query.
- **uvicorn's loggers do not propagate.** `uvicorn.error` and `uvicorn.access` install their
  own plain-text handlers, so an exception escaping an ASGI app was logged outside the JSON
  stream whatever the app configured. `jutsu_core.logs.configure` takes them over by name.
  It also merges a dict message (`logger.info("%s", {...})` — the convention here) into the
  JSON object, so `jsonPayload.event` is a filter rather than a substring search.
- **`claimable_now` counts rows in a claimable *state*, not work the drain can do.**
  `drain_org` skips `embed.document` entirely when no embedding provider is configured,
  so those rows stay `pending` — correct and documented — and a follow-up decision made
  on the count alone rings every five seconds for ever against a table nothing changed.
  Measured: a two-file ingest with Vertex unset left two pending rows and re-rang on
  both. `drain_and_report` therefore requires **progress** before it will ring `now`;
  with none, recovery is the next real doorbell (sign-in, Jobs page, next enqueue).
- **A caller-supplied `drain_url` is an address, not a configuration.** The worker
  always knows its own, so `CloudTasksDoorbell.from_env(drain_url=…)` must decide "is
  this transport in use" from the environment alone. Counting the argument made the
  unconfigured dev door raise `MisconfiguredDoorbell` — answering 500 for a follow-up
  ring on a drain that had already committed, which is precisely what "best-effort,
  never a failure" forbids. Every `_doorbell_for` test stubbed it, which is how it got
  that far.
- **`/healthz` never reaches a Cloud Run container — Google's frontend answers it.**
  Verified on `jutsu-api`: `/nonexistent-abc` and `/v1/nope` come back as the app's JSON
  404 carrying `x-request-id`, while `/healthz` returns a Google HTML 404 with no such
  header. `/readyz` is untouched, which is why `deploy.yml` verifies on that one. The
  worker exposes `/healthz` too and its deploy check reads Cloud Run's own Ready
  condition rather than calling it, so nothing is broken — but do not debug a
  production liveness route by curling it, and do not "fix" a 404 there in the code.
- **`create_app()` must not log.** `scripts/emit-openapi.py` writes the schema to
  stdout and `_configure_logging()` points the root handler at stdout too, so one line
  emitted at construction lands *inside* `openapi.json` and makes it invalid JSON —
  `make api-types-check`, and with it the commit gate, fails on a file whose schema is
  perfectly correct. Startup facts belong in the app's `lifespan`, which the schema
  emitter never runs.
