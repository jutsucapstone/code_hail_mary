# ADR 0011 — Authorization is a predicate in the retrieval query

- **Status:** accepted
- **Date:** 2026-08-28
- **Slice:** S7

## The sentence this ADR exists to make true

> **A user must never retrieve evidence unless they are authorized to see that evidence,
> and the authorization must happen before the evidence reaches the application.**

## Context

§12 specifies the query, down to the SQL. §17 specifies seven adversarial tests. Neither
says where the *inputs* to that query come from, and that turned out to be the entire
design question — because every way of getting them wrong produces a system that passes
its own tests.

Three failure shapes were available, and all three look fine in review:

- **Post-filtering in Python.** Fetch a wider set, drop what the caller may not see. The
  returned rows are identical to the correct implementation, so every outcome-based test
  passes. It leaks through counts, through `LIMIT`, and through pagination.
- **Passing the principal set in.** Resolve principals once, hand them to the search as an
  argument. Correct at every call site that exists today, and one future call site away
  from a caller that passes a wider set or a stale one.
- **Taking `org_id` as a parameter.** The tenant becomes something a call site names,
  which means it is one refactor away from being something a request names.

## Decision

### 1 · The filter is `ACL_PREDICATE`, a module constant, inside the SQL

```sql
EXISTS (SELECT 1 FROM document_acl a
        WHERE a.document_id = d.id AND a.permission = 'read'
          AND ((a.principal_type='user'  AND a.principal_id = ANY(:principals))
            OR (a.principal_type='group' AND a.principal_id = ANY(:groups))
            OR (a.principal_type='org'   AND a.principal_id = CAST(<org GUC> AS text))))
```

§12's filter, with ADR 0010's single change — `= $3` became `= ANY(:principals)`, because
a user holds one subject per source system rather than one portable id.

It is a **constant**, not a string built per call, for two reasons that are both tested:
the escalation ladder can be proven to re-run the same predicate, and `evidence.py` shares
it rather than restating it. Two spellings of an authorization check is how two
authorization checks come to disagree.

### 2 · There is no `principals` parameter and no `org_id` parameter

`search_chunks(session, *, user_id, query_vector, k, after, …)`.

Principals are resolved **inside the function**, in the caller's transaction, from
`jutsu_db.acl.resolve_acl_principals`. The tenant is read from `app.current_org_id` — the
same GUC every RLS policy compares against, with the same `NULLIF` so that unset and reset
both fail closed.

This is stronger than passing them in, and the strength is structural rather than
disciplinary: there is no argument through which a call site could widen either. It also
makes revocation immediate by construction, because there is no set that could be stale.
A test asserts the signature, because a parameter appearing there later is exactly the
regression to catch — the same reason ADR 0010 pins `scoped_acl_principals`.

The cost is one indexed read per search. It is the right price, for the reason ADR 0010
already paid it once.

### 3 · The resolver moved to `jutsu_db`, and the API delegates

`apps/api` and `packages/retrieval` need the same answer, and `packages/retrieval` cannot
import from `apps`. `scoped_acl_principals` keeps its name, its signature and its tests,
and now calls `jutsu_db.acl.resolve_acl_principals`.

### 4 · A short result set is answered by searching harder, never by filtering less

HNSW returns roughly `ef_search` candidates and *then* the filter runs, so a restrictive
ACL can leave far fewer than `k`. Three mechanisms, none of which touches the predicate:

- **`hnsw.ef_search` ladder — 100 → 400 → 1000.** §12 opens at 100; 1000 is pgvector's own
  ceiling, measured, and a larger value is rejected by the server rather than clamped.
- **`hnsw.iterative_scan = strict_order`** (pgvector 0.8+). The index keeps scanning until
  the limit is met instead of stopping at the first `ef_search` candidates. `strict_order`
  rather than `relaxed_order` because the keyset cursor depends on exact distance order.
- **`hnsw.max_scan_tuples`**, so a filter matching almost nothing cannot turn every search
  into a sequential scan wearing an index's clothes.

Escalation stops on three conditions: `k` reached, ladder exhausted, or **a wider search
found nothing new**. That last one is what keeps the most restricted caller from paying the
highest price on every query — when somebody is authorized to see six documents, no amount
of searching finds a seventh.

The cost is one extra probe whenever fewer than `k` rows come back, and it is the probe
that distinguishes *"you are not authorized to see more"* from *"the index did not look
hard enough"*. Without it the second silently masquerades as the first.

### 5 · `public` grants are deliberately not honoured

Migration 0001's check constraint permits `principal_type = 'public'`. §12's filter does
not mention it, and nothing writes it. A grant type whose semantics nobody has defined is
treated as no grant, so such a document is **invisible rather than visible to everyone**.

Fail-closed is the right direction and the behaviour is pinned by a test rather than left
to be inferred. The failure mode is worth naming: if a connector later emits `public`, those
documents will silently not be retrievable. Giving `public` a meaning is an ADR, not a patch.

### 6 · The query is a CTE, and the shape was measured rather than assumed

```sql
WITH hits AS (
  SELECT c.id, …, c.embedding <=> :query AS distance
  FROM chunks c                          -- chunks ALONE
  WHERE c.org_id = <GUC> AND c.embedding IS NOT NULL
    AND EXISTS (SELECT 1 FROM documents d WHERE d.id = c.document_id
                  AND … AND <ACL_PREDICATE>)
  ORDER BY c.embedding <=> :query        -- distance ONLY
  LIMIT :k)
SELECT h.*, d.title, s.system FROM hits h JOIN documents d … JOIN sources s …
ORDER BY h.distance, h.id
```

The first draft was §12's SQL literally — `FROM chunks JOIN documents JOIN sources … ORDER
BY distance, c.id LIMIT k`. It is correct, every adversarial test passed against it, and on
40 000 chunks with an organisation-wide grant it took **3 016 ms**, because the HNSW index
was never opened. Two independent causes, isolated by bisecting the query:

| Inner scan | HNSW used | 40k chunks, permissive ACL |
|---|---|---|
| joins in the `FROM` | no | 3 016 ms |
| `chunks` alone, `ORDER BY distance, c.id` | no | 203 ms |
| `chunks` alone, `ORDER BY distance` | **yes** | **15 ms** |

- **Joins drive the plan.** With `documents` in the `FROM`, the planner drives the nested
  loop from `documents` and the vector index is unreachable. They move to the outer
  projection, over the `k` rows the scan already authorized.
- **An index supplies exactly one ordering.** A secondary sort key forces a full sort of
  every qualifying row, so `, c.id` inside the scan abandons the index.

**The tie-break is not lost — it moved outward**, where it sorts `k` rows instead of the
corpus. What that costs, stated plainly: *which* rows are chosen when several tie at exactly
the `k` boundary is left to the index. Nothing above `k` is affected, and HNSW is
approximate at that boundary regardless.

Neither cause fails a test, changes a row, or looks wrong in review. Two string assertions
pin the shape, because a string assertion is the only kind that catches a regression whose
entire symptom is being a hundred times slower.

**Pagination** is a keyset on `(score, chunk_id)`, applied as another `AND` **inside** the
scan so the `LIMIT` still lands after authorization. Not an offset: an offset re-runs the
query and skips rows, so a document becoming visible between pages silently shifts the
window. A cursor carries **no authorization** — the next page re-resolves principals and
re-applies the same predicate, so a forged one grants nothing.

### 7 · The permission names the query, not the evidence

`retrieval:query`, held by **every** role including a bare Member. §17: roles gate
features, ACLs gate data. Asking the corporate memory a question is the feature every
employee is hired to use; `document_acl` decides what comes back.

It was drafted as `evidence:read` and `test_no_permission_grants_document_visibility`
refused it. **The refusal was correct**, and the lesson is worth more than the rename: a
permission spelled after a data-plane object reads as a grant over that object however its
docstring argues otherwise, and the next person to add one follows the precedent.

Widening the catalogue needs a migration (0009), because migration 0002 revoked application
writes to `permissions` and `role_permissions`. A compromised handler cannot grant itself
retrieval.

### 8 · `GET /v1/evidence/{chunk_id}` answers 404, never 403

Fetch-by-id is the second door onto the same evidence, and it is the door where
authorization is easiest to forget — it looks like plumbing next to the obviously
security-critical search. It therefore imports `ACL_PREDICATE` rather than re-deriving a
check.

A 403 would confirm the chunk exists, which turns the endpoint into an oracle: walk ids,
read the status codes, recover the shape of a tenant's corpus without ever being authorized
to read a word of it. `NotFound` is the single answer for never-existed, another-tenant's,
and not-granted-to-you.

## Consequences

- **The `EXPLAIN` test is the one that matters.** Every other test asserts an outcome, and
  a Python post-filter produces identical outcomes until a count or a `LIMIT` is involved.
  §17 test 7 asserts the *mechanism*, and it is what catches a future refactor moving the
  filter out of the database "for readability".
- **Sabotage was used to prove the suite is not vacuous.** Replacing `ACL_PREDICATE` with
  `TRUE` fails 22 of the 37 adversarial tests. A suite that stays green against a design
  that leaks is the most expensive thing this repository can contain, and asserting that it
  goes red is the only way to know it does not.
- **A caller with no source identity retrieves nothing** — except organisation-wide grants,
  which are a deliberate statement about everyone in the tenant rather than an absence of
  one. That is §17 test 1 and §17 test 4 coexisting, and both are tested.
- **Scoping a session to another tenant returns nothing rather than that tenant's data.**
  There is no `org_id` argument to tamper with, and RLS then hides the caller's own user
  row too, so they resolve to no principals. Both halves fail closed.
- **The audit trail does not record retrievals.** §17 audits person-level risk reads,
  handover generation and ACL changes; a search is none of those, and one row per query
  would drown the trail that matters. Observability is a structured log line carrying
  counts, `ef_search`, attempts and latency — never the question, the text, a vector or a
  principal, since a principal is a provider subject and therefore personal data (§4.9).

## What this does not solve

- **No fusion, no rerank, no graph expansion.** §12's full pipeline is S9. This is the
  vector half, and it is the half the ACL filter lives in.
- **Nothing embeds a question yet.** `search_chunks` takes a vector. Turning a question
  into one is `EmbeddingTask.QUERY`, and the caller that does it is `/v1/ask` in S9 — which
  is also why there is no search *route* here. §4.11 forbids shipping a surface that is not
  real, and a search endpoint with no query embedding would be exactly that.
- **The graph side is still untouched.** §4.6 requires a fact whose evidence is entirely
  ACL-invisible to be invisible. Nothing here filters graph evidence; that needs the same
  predicate applied to Neo4j-derived claims, and it is a slice of its own.
- **`pii_vault` still has no RLS.** Unchanged from ADR 0010, still a separate defect.
- **Measured at 40 000 chunks, on this machine, in both ACL shapes** — close to M1's 45k
  target but on a dev container, so these are indicative and not a budget:

  | Caller's grants | plan | k=30 latency |
  |---|---|---|
  | organisation-wide (all 8 000 docs) | HNSW index scan | 0–32 ms |
  | 1-in-40 documents | ACL-first, exact sort of 1 000 authorized chunks | 16–61 ms |

  **The planner picks a different plan per shape and both are correct.** A restrictive ACL
  makes the authorized set small enough to rank exactly, which is *better* than an
  approximate index scan — no recall loss. A permissive one makes the index worth opening.
  `document_acl` appears in both plans.

  The consequence for the ladder is worth naming: **in the ACL-first plan `ef_search` is
  inert**, because no index is being scanned. It is not dead code — it governs the
  permissive shape, which is the one that would otherwise scan the whole corpus — but a
  reader should not assume the ladder is what makes restrictive queries fast. The exact
  plan is.
- **These are dev-container numbers on synthetic random vectors.** Real embeddings cluster,
  which changes HNSW's traversal, and Cloud SQL is not this machine. A latency budget is an
  S8 measurement against the real corpus, not a claim this ADR is entitled to make.
