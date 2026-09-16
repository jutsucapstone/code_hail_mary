# ADR 0030 — Cited Q&A reads the asker's own memory

**Status:** accepted
**Date:** 2026-09-16
**Related:** ADR 0010 (namespaced principals), ADR 0011 (no principals parameter), ADR 0022
(additive GraphRAG), ADR 0025 / ADR 0027 / ADR 0028 (knowledge transfer), ADR 0029 (folders)

## Context

An employee asked production "tell me about my project from github astro agent" and was told
*"The evidence you are authorised to read does not answer this."* That sentence was wrong about
why, and the product gave them nothing to act on.

Measured, from production logs:

| Observation | Evidence |
| --- | --- |
| The refusal was the no-evidence branch, not a ranking or model failure | `vector_search returned=0 k=30 attempts=2 ef_search=400 elapsed_ms=85 exhausted=True`, then `POST /v1/ask` 200 in 0.71 s with no model call |
| The asker's organisation has no connected application at all | `scheduled_sync … connections=0 enqueued=0` for that org on each of 2026-09-12…15 |
| Nothing has ever been ingested there | no `source_run`, `embed_document` or `job_failed` line for that org since 2026-09-01 |
| Other organisations do answer | another tenant: `vector_search returned=30 … exhausted=False`, `connections=9 enqueued=9` |

So retrieval and authorization were correct and the corpus was empty. Three things were wrong
anyway:

1. **One sentence for two different facts.** "The evidence does not answer this" and "nothing you
   may read here is searchable yet" send a person to opposite actions, and only the second was
   true.
2. **Passages only.** Extraction writes quote-gated claims — projects, decisions,
   responsibilities, people — and Ask KT reads them (ADR 0028) while Cited Q&A did not.
3. **No way back to the source.** A citation opened its passage and stopped there; the address
   the connector already stored on the document was never shown.

Sign-in also opens an identity's **oldest** membership (`auth.resolve_memberships` orders by
`created_at`), so an employee who belongs to two organisations can be asking inside the empty
one while their connectors sync into the other.

## Decision

### 1. The signed-in employee is the scope, and nothing in the request may name one

`/v1/ask` forbids unknown fields, and there is no field for an organisation, a person, a project,
a source, a document or a date. Every arm resolves the caller's principals inside its own SQL
from the session's user id, under `ACL_PREDICATE`, in the tenant the session GUC names.

Three arms, one boundary:

- **Passages** — `search_chunks`, unchanged.
- **Claims** — `jutsu_retrieval.claims.search_claims`, new (below).
- **Folders** — `search_folders`, unchanged (ADR 0029).

**No knowledge-transfer concept appears anywhere in this path.** Not the package predicate, not a
subject, not a recipient, not a KT code. KT answers a different question — "what did *this other
person* know" — through its own implementation, and a test asserts `routers/search.py` imports
nothing from it.

### 2. The claims arm is two bounded candidate arms and one ordering

- **Anchored:** claims whose evidence chunk is one the vector search just returned
  (`cl.chunk_id = ANY(...)`, at most `MAX_ANCHORS` = 60).
- **Intent:** when the question names a kind of claim, the most recent `INTENT_WINDOW` = 200
  claims of that kind among the caller's documents (`cl.claim_type = ANY(...)`).

Both are equality over indexed columns, which is what row-level security allows as an index
condition. The question's words then rank the bounded pool by `ts_rank`, and at most
`CLAIM_LIMIT` = 12 claims reach an answer.

A claim counts only when its run is the latest **finished** extraction run for its document, and
that lookup is computed once for the candidate documents rather than per claim.

**Why not KT's statement.** KT joins the latest run with a correlated subquery keyed on
`stats_json->>'document_id'`. `->>` is not leakproof, so beneath a policy that index serves the
owner and never `jutsu_app`. Measured on 5,000 documents / 16,500 claims, same data, same
question:

| Shape | as `jutsu_app` | as owner |
| --- | --- | --- |
| KT's claims statement under `ACL_PREDICATE` | **27,458 ms** | 137 ms |
| This statement | **45 ms** | 33 ms |

At 50,000 documents / 165,000 claims, as `jutsu_app`: **129 ms** for a question naming a kind of
claim, **41 ms** for one that does not, against 610 ms for an unscoped latest-run pass. KT's own
statement keeps its shape here and is being fixed separately; nothing in this ADR changes it.

### 3. A link to the original is the source's address or nothing

`documents.uri` reaches a citation only through `jutsu_retrieval.links.safe_source_uri`: an
absolute `http`/`https` address, with a host, no credentials, no whitespace or control
characters, at most 2,048 characters. Anything else — a `javascript:` scheme, a file path from a
local corpus, a relative fragment — is no link at all. A folder cites its own `folder_uri` under
the same rule. **JUTSU never composes an address**, so a citation cannot point somewhere that
does not exist, and the evidence panel still opens the passage either way.

### 4. Two refusals, because they are two facts

`refusal_reason` is `no_authorized_evidence` when no arm returned anything, and
`evidence_does_not_answer` when evidence was retrieved and no answer could be grounded in it.

This is **not** the existence oracle §4.5 forbids. Both statements are about the caller's own
authorized reach: the first says *your* scope here is empty, which they can already establish by
asking anything at all. Neither says whether a document they may not read exists.

The web surface renders the first as which organisation this session is in, plus where
searchable content comes from — connected applications, the Knowledge Basket, what colleagues
share.

### 5. The claims arm may fail without failing the answer

It runs inside a savepoint. A statement timeout on a tenant large enough to reach one costs the
answer its claims and is logged (`ask_claims_skipped`), because Postgres aborts a whole
transaction at its first error and the folder search shares it.

### 6. Observability is counts and timings

`ask_completed` carries passages, claims, folders, citations, attempts, the refusal reason and
the per-stage milliseconds. No question, no passage, no claim, no title, no address, no
principal.

## Consequences

- **A person with an empty tenant is told so**, and sent to the two places content comes from,
  instead of being told the evidence disagreed with them.
- **Structured questions find structured answers.** "Which decisions did I make?" reads D's
  decision claims even when the passage recording them ranks nowhere near the question — and
  even when that passage has not been embedded yet.
- **Citations open the original** wherever the connector stored an address: GitHub, Drive,
  SharePoint, Zoom, Jira. Slack, the Knowledge Basket and the local corpus have none, and show
  none.
- **Cost:** one extra bounded statement per question, plus up to twelve numbered items in the
  prompt. Measured above.
- **Residue — the intent window.** A claim that is neither extracted from a retrieved passage nor
  among the 200 most recent of its kind is not read by the intent arm. Measured: with an index on
  `documents (org_id, created_at DESC)` the same statement runs in 44 ms instead of 129 ms at
  50,000 documents, which would let the window grow; no tenant is near that size, so the index is
  not built.
- **Residue — one membership.** Sign-in opens the oldest membership and there is no organisation
  switcher, so an employee whose connectors live in another tenant reads an empty scope here.
  The empty state names the organisation, which is the fact they need; choosing between
  memberships is a separate change to authentication.
- **Residue — KT's claims statement.** The 27 s shape above is still what Ask KT runs. It is
  recorded here because it was measured here, and it is not changed here.

## Alternatives considered

**Reuse KT's subject/package retrieval for the employee's own questions.** Rejected outright: it
resolves a *different* person as the subject, carries package exclusions and a package period,
and would hand an employee a view assembled for somebody else's handover.

**Filter claims in Python after a wider read.** Rejected — non-negotiable 5. It also makes counts
and limits leak what a filter is meant to hide.

**An unbounded full-text arm over every claim.** Measured 610 ms at 50,000 documents and grows
with the tenant; the anchored arm covers the same questions through the passages that carry them.

**Embed claims and search them by vector.** The same answer as ADR 0028: better recall on
paraphrase, at the cost of a second vector column, an embedding job per extraction run and a
model-version story. Not built.
