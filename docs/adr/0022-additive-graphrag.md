# ADR 0022 — GraphRAG as an additive retrieval layer

**Status:** accepted
**Date:** 2026-09-12
**Related:** ADR 0002 (denormalised org_id), ADR 0007 (graph tenancy), ADR 0011
(ACL-filtered vector retrieval), ADR 0012 (ingestion jobs), ADR 0017 (production
background execution)

## Context

`packages/graph` has existed since S2: a driver, an org-scoped session that refuses an
unscoped query, a label allowlist, numbered Cypher migrations and bitemporal helpers. It
has 125 tests and no callers. Nothing in `apps/api` or `apps/worker` imported it, neither
declared it as a dependency, and `/readyz` reported `neo4j: not_configured` honestly
because the gateway genuinely took no dependency on it.

Meanwhile retrieval is vector-only: `search_chunks` ranks chunks by cosine similarity with
`ACL_PREDICATE` inside the SQL, and `/v1/search`, `/v1/ask` and the KT copilot all go
through it. That path works, is in production, and is the thing people use.

Spec §12 describes something larger — intent classification, a Cypher template per intent,
dual retrieval, one-hop expansion of vector hits, reciprocal rank fusion at k=60, a
cross-encoder rerank — and the roadmap puts it in Phase 3. The question this ADR answers
is not whether to build that; it is **how to add the graph half without putting the
working half at risk.**

## Decision

GraphRAG is added as a layer *beside* vector retrieval, never in front of it.

**1. The vector search runs first and unconditionally, and its result is the floor.**
`jutsu_api.graphrag.retrieve` calls `search_chunks` with the arguments it has always
taken, then — only if the graph is enabled, configured, reachable and quick — fuses in
what the graph suggests. Five failure modes (flag off, no connection details, a timeout, a
raised exception, a paginated request) land in one place: the vector result, with a reason
attached. The fallback is not an error path that has to work; it is the absence of an
enhancement.

**2. Neo4j is never an authorization surface.** Graph retrieval returns `chunk_id` and
`document_id` — identifiers, nothing else. Those identifiers go to
`jutsu_retrieval.evidence.fetch_evidence_many`, which runs the same `ACL_PREDICATE`
against the caller's own resolved principals in the same transaction, and returns only
what came back. A candidate the caller may not read is absent: no error, no count that
names it, nothing reaching the model. Postgres remains the authority for authorization,
exactly as before.

**3. The graph stores identifiers and entity names. It never stores passages.** This is a
deliberate narrowing of §7's node properties. Neo4j has no row-level security (ADR 0007);
Postgres does. So an edge carries `chunk_id`, `document_id`, offsets, a confidence and an
extractor version — enough to recover the evidence — and never the quote. Document nodes
carry no title for the same reason: retrieval reads it from Postgres under the ACL, and a
copy outside that boundary would be a second place to leak from with nothing to gain.

What *is* outside Postgres is the entity name: a person's name, a project's name, a
decision's statement. A knowledge graph that cannot name its entities can neither be
traversed nor matched against a question. That exposure is real and bounded, and it is the
one thing this decision trades away.

**4. Every node and edge comes from an extraction claim, and nothing else does.** No
relationship is inferred from co-occurrence, proximity, or an LLM's opinion about the
shape of an organisation. `extraction_claims` rows already carry a `chunk_id` that is NOT
NULL and a quote the hallucination gate proved verbatim; `jutsu_graph.knowledge` maps
those to nodes and edges and refuses a claim type it does not know. "Why does JUTSU believe
this relationship exists" is answerable for every edge in the store.

**5. Identifiers are derived, so idempotence is structural.** A node's key is a hash of
`(label, normalised name)`; an edge's id is a hash of
`(org, relationship, source, target, chunk)`. Re-running extraction MERGEs onto the same
ids. A re-extraction that no longer supports an edge **closes its validity interval**
rather than deleting it (§7).

**6. Graph ingestion is a job of its own, last in the chain.**
`ingest.document → embed.document → extract.document → graph.document`. It is enqueued
only when `NEO4J_URI` is set, and a failure is recorded on its own row: the document is
ingested, the chunks are embedded, the claims are extracted, and pgvector has been
answering questions about it since the embedding committed.

**7. Two gates, deliberately separate.** `NEO4J_URI` decides whether the *worker*
projects claims into the graph. `GRAPHRAG_ENABLED` decides whether *retrieval* reads from
it. The graph therefore fills up while answers are still pure vector, which is the
rollout: configure, populate, verify, then enable.

**8. Fusion is by rank, not by score.** A cosine similarity and an extractor's confidence
are both numbers in `[0, 1]` and mean entirely different things. RRF at k=60 (§12's
constant) discards the magnitudes and keeps the positions.

## Consequences

**The existing system is unchanged when the graph is absent.** With no `NEO4J_URI` and the
flag unset — which is every environment until somebody changes it — `retrieve` is
`search_chunks` plus a reason string, no Neo4j connection is attempted, no graph jobs are
enqueued, and `/readyz` reports `not_configured` as it always has.

**A graph outage is not an outage.** `/readyz` reports `neo4j: degraded` and stays
`ready`, because only `failed` means an outage and only Postgres can say it. The probe is
bounded at two seconds and cached, so an unreachable host cannot make readiness slow
enough to fail a deploy over an optional dependency.

**`/v1/search` keeps its cursor contract by defaulting to vector.** `next_cursor` is a
keyset over the vector ordering, and a fused list is not in that order — a fused first
page followed by a vector second page can skip a passage that fusion displaced. So the
paginated surface defaults to `retrieval_mode=vector` and a caller who is not paginating
asks for `hybrid` explicitly. `/v1/ask` returns one bounded set, never paginates, and
defaults to `auto`.

**There is no reranker.** §12 ends with a cross-encoder over the fused candidates. There
is no cross-encoder in this deployment, and sorting on a similarity that RRF deliberately
discarded would be worse than not reranking at all. The fused order reaches the model,
truncated to `k`. This is the largest remaining gap against §12 and it is stated rather
than implied.

**There is no entity resolution.** `entity_key` normalises case and punctuation and
nothing more, so "Ada" and "Ada Lovelace" are two Person nodes. Fuzzy matching belongs in
§11's `resolution_queue`, with a human above the threshold — a silent merge inside a key
function would be irreversible and wrong in a way nobody could see.

**Query understanding is deterministic and weak.** Entity seeding matches word n-grams
from the question against names the graph holds. It cannot invent an entity that was never
extracted, needs no model call on the request path, and is testable without a fixture. The
cost is recall: "the Alpha project" does not match an entity stored as "Project Alpha".

**A stale edge resolves to nothing, not to stale evidence.** Document nodes are keyed by
`(org_id, source_system, external_id)` — the document's identity across versions — and
edges carry the version they were extracted from. A superseded version's chunks fail
`d.superseded_by IS NULL` in the ACL fetch, so an edge nobody re-asserted simply stops
producing candidates.

**Postgres remains the source of truth and the graph is a projection.** Losing Neo4j
entirely loses nothing that cannot be rebuilt by re-running `graph.document` jobs, which
is why they can be dropped, retried and re-ordered freely.

## What was rejected

**Putting the graph in front of retrieval.** An intent classifier choosing between paths
makes the graph a dependency of every question, including the ones pgvector answers
perfectly. The failure mode is a total outage caused by an optional store.

**Letting the graph decide visibility.** Copying `document_acl` into Neo4j and filtering
there. Rejected on ADR 0007's finding: Neo4j Community has no row-level security and not
even property-existence constraints, so the copy would be enforced entirely by application
code with nothing underneath it — and it would be a second authorization system to keep in
step with the first, which is how the two disagree.

**LLM-generated Cypher.** §22 anticipates it and it is not built here. Every query is a
constant in `jutsu_graph.retrieval`; caller text reaches the query only as a bound list of
strings. When a generator does land it needs a procedure allowlist on top of `labels.py`,
because neither the write-clause check nor `READ_ACCESS` classifies a `CALL`.

**Storing quotes on edges to save a Postgres round trip.** It would put tenant content in
a store with no row-level security, to avoid a fetch that is also the authorization check.
The round trip is the point.
