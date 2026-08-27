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
  toward the limit on the assumption that it over-counts.
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
