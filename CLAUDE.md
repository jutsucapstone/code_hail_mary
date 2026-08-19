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
- **HNSW plus a restrictive ACL filter returns fewer than `k`.** Raise `ef_search` and
  over-fetch. Never "fix" a short result set by loosening the filter.
- **Random-sampling Enron destroys the reply graph.** Sample complete threads or entity
  resolution has nothing to resolve — and you will not notice until week 5.
- **Entity merges must be reversible.** Write `ALIAS_OF`; never destructively merge two Person
  nodes. A wrong irreversible merge silently corrupts every downstream score.
- **LLM-generated Cypher runs read-only**, with a statement timeout and a label allowlist.
- **Query and document embeddings use different `task_type` values.** Using one for both costs
  several points of recall and is invisible until eval.

### Landing-page traps (`apps/web`)

- `--brand` / `--graph` **flip lightness role between themes**; `--brand-foreground` inverts to
  match. Re-check contrast in **both** themes after any palette edit.
- The JUTSU wordmark is **vector-traced from the supplied artwork** (`lib/wordmark-paths.ts`),
  never a font. Regenerate with `node scripts/trace-wordmark.js`.
- FAQ and architecture panels **stay mounted when collapsed** (`inert` / `hidden`). Unmounting
  breaks `aria-controls` and hides answers from crawlers.
- Full-res logo lives in `assets/` and is **not served**. `public/jutsu-logo.png` is a generated
  256px copy — putting the 1254px original back costs ~900KB of deploy weight.
