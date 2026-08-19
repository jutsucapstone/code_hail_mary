# Phase 1 · Weeks 1–3 — Foundation & Ingestion

Spec: `docs/jutsu-master-spec.md` §21. Lanes: **D** data · **B** graph/backend · **A** AI/RAG ·
**F** frontend.

`S1`–`S3` can run in parallel once `S0` lands. **`S4 → S5 → S6 → S7` is the critical path** —
protect it; everything in Phase 2 depends on chunk offsets and the ACL filter being right.

| Slice | Lane | Content | State |
|---|---|---|---|
| S0 | F | Monorepo (pnpm + uv workspaces), Compose, CI, landing page folded into `apps/web`, product routes stubbed behind auth | **in progress** |
| S1 | B | Postgres migration 001 + pgvector + RLS (§8) | not started |
| S2 | B | Neo4j migration runner + constraints + `temporal.py` (§7) | not started |
| S3 | D | Corpus loaders, connector protocol, Enron thread sampler (§19) | not started |
| S4 | D | PII masking + offset map, ten tests (§9.1) | not started |
| S5 | D | Chunker with original-document offsets (§9.2) | not started |
| S6 | A | Embedder + HNSW (§9.3) | not started |
| S7 | B | ACL capture + filtered search + 7 adversarial tests (§12, §17) | not started |
| S8 | B | Job queue, idempotent pipeline, crash resume | not started |
| S9 | A | Gate harness `scripts/gate.py --phase 1` | not started |

---

## S0 — Monorepo foundation

**Goal:** the skeleton every later slice writes into, with the landing page moved in and still
building identically. No product behaviour ships.

### Deliverables

- `git` initialised, baseline commit of the landing page before any file moves.
- `docs/jutsu-master-spec.md` (full spec), `docs/plan-phase-1.md` (this file), thin `CLAUDE.md`
  carrying only §2 / §4 / §5 / §22 and the trap list.
- `.claude/settings.json` — `PreToolUse` hook running `make preflight` before `git commit`
  (§4.15; the spec notes enforcement belongs in hooks, not prose).
- pnpm workspace: `apps/web`, `packages/ui`.
- uv workspace: `apps/api`, `apps/worker`, `packages/{core,graph,retrieval,agents,connectors,evals}`.
- `apps/web` — landing page moved verbatim, then split into `(marketing)` / `(product)` route
  groups with six stubs behind stub auth in `middleware.ts`.
- `packages/ui/tokens.ts` — design tokens lifted out of `globals.css`, imported by both groups.
- `packages/core` — the §9 Pydantic models S3–S5 will import.
- `apps/api` — FastAPI with `/healthz`, `/readyz`, typed error envelope, `request_id` middleware.
- `infra/docker/compose.yml` — pgvector, Neo4j CE, Redis. Written now, first run at S1.
- `Makefile`, `.github/workflows/ci.yml`, extended `.env.example`.

### Gate S0

1. `pnpm --filter web build` — same 10 routes as the pre-move baseline, lint + typecheck clean.
2. Landing page renders identically at :3210 — 8 sections, no horizontal overflow, contrast
   unchanged in **both** themes.
3. `/ask` unauthenticated redirects to `/`; with the stub cookie it renders the stub.
4. `uv run pytest` green across all Python packages.
5. `curl localhost:8000/healthz` → 200 carrying a `request_id`.
6. `make preflight` green.
7. A `git commit` with failing lint is blocked by the hook.

### Out of scope

No connectors, ingestion, migrations, LLM calls or real auth. Those are S1–S9.

---

## Gate M1 — end of Phase 1

Every item measured, not asserted (§22.8):

- ≥45k documents ingested from the Enron sample.
- A second `make seed` adds **zero** rows — idempotency on
  `(org_id, source_system, external_id, content_hash)` proven, not assumed.
- Every chunk offset resolves to matching text in the **original** document body.
- Zero raw PII in captured logs.
- 100% of chunks embedded at dim 768.
- All 7 adversarial ACL tests pass, including the `EXPLAIN`-contains-`document_acl`-join test.
- Cross-org isolation proven.
- Migrations reversible — `downgrade` then `upgrade` returns an identical schema.
- `make preflight` green with ≥70% coverage on `core`, `graph`, `retrieval`.
- Seed-run token cost recorded in `extraction_runs.stats_json`.

---

## Notes carried into Phase 1

- **Bitemporality starts at migration 001** (§7). `packages/graph/temporal.py` with
  `supersede()` and `as_of()` is written in S2, before there is anything to store. Retrofitting
  it in week 8 means rewriting every edge and template, and the decision ledger is worthless
  without it.
- **Enron sampling takes complete threads**, never random messages (§19). The sampler emits
  `sample_manifest.json`; the same seed must produce a byte-identical manifest.
- **PII masking is the hardest correctness problem in the ingestion path** (§9.1). Its ten tests
  are written *before* the implementation, and they include the non-ASCII case — character
  indices, not byte indices.
