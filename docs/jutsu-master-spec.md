# JUTSU — Corporate Memory Graph · Complete Master Prompt

Enterprise Memory OS. Team Code Hail Mary · Manipal University Jaipur · Deloitte Capstone 2026.

This is the whole project in one document: product spec, data model, contracts, agents, UI,
security, evals, deployment, and a twelve-week build plan. Everything an agent or an engineer
needs to build JUTSU without asking what was meant.

> **Do not paste this into `CLAUDE.md`.** It is far past the ~200-line threshold where instruction
> files start losing adherence. Keep §2 (invariant), §4 (non-negotiables), §5 (stack) and §22
> (working agreement) in `CLAUDE.md`; keep this file in `docs/` and reference it by path so it
> loads on demand. Enforcement that must not be talked past belongs in hooks, not in prose.

---

## Amendments

Deviations from the original document, agreed with the team. The spec text below is otherwise
unchanged; these override it where they conflict.

| # | Section | Amendment | Reason |
|---|---------|-----------|--------|
| A1 | §5 Stack | **Next.js 16.3.1**, not 15. | The landing page was built, audited and shipped on 16 before this spec landed. Downgrading is destructive with no benefit. |
| A2 | §16 Design system | Tokens are **dark-first with a light mode**, brand green sampled from the logo (`#83d005` / `#499f02`), `--brand`/`--graph` flipping lightness per theme. Not `#2F7D32` on `#FAFAF8` paper. | §16 claims to inherit from the existing landing page but lists values that page never used. The built tokens are real, shipped and WCAG AA verified in both themes. |

---

## 1 · Mission

Companies forget. Institutional knowledge sits in emails, chats, decks, tickets and heads, and it
walks out the door with every resignation, re-org and project roll-off. Fortune 500 firms lose
~$31.5B/yr to poor knowledge sharing (IDC); ~19% of the work week goes to searching for
information (McKinsey); ~42% of role-critical know-how is unique to a single person (Panopto).

The gap is not storage — every file is saved. The gap is **memory**: who did what, why, and what
changed.

JUTSU connects read-only to the systems an organisation already runs, extracts people, projects,
decisions, meetings and skills into a temporal knowledge graph, and serves six surfaces over it.
Nothing is written back. No migration. No change to how anyone works.

---

## 2 · The one invariant

**A fact is only true in JUTSU if it points at the evidence that produced it, and only visible if
the caller can see that evidence.**

Every design decision serves that sentence. If a change weakens provenance or ACL fidelity, it is
wrong regardless of how much faster or cleaner it is. When you are unsure what to do, work out
which option keeps that sentence true and take it.

---

## 3 · Product — six surfaces

| # | Surface | Must actually do | Status |
|---|---------|------------------|--------|
| 1 | **Cited Q&A** | Plain-language question → grounded answer, every claim clickable to a highlighted source span. Refuses rather than guesses. | table stakes, done well |
| 2 | **Decision Ledger** | "Why did we choose PostgreSQL over MongoDB?" → the decision, its date, who decided, the meeting or thread it happened in, what superseded it. | **differentiator** |
| 3 | **Expert Discovery** | Topic → ranked humans scored on demonstrated contribution, not self-declared CV skills. | **differentiator** |
| 4 | **Knowledge Risk** | Live bus-factor per project and topic. Where knowledge concentrates in one head. Aggregate-first. | **differentiator** |
| 5 | **Handover Studio** | One click → cited leaver/joiner pack: open items, key decisions, stakeholders, gotchas. Under 60 seconds. | **differentiator** |
| 6 | **Onboarding Copilot** | "What should I read first for Project Falcon?" → ordered reading path built from the graph. | table stakes |

Glean, Microsoft Viva, Guru and Atlassian Rovo index and search documents. None keep a temporal
decision ledger, score bus-factor risk, or auto-draft cited handover packs. **The moat is the
graph of who-decided-what-when, not another index.** Any feature achievable with plain vector
search over a document store is table stakes — build it, but never mistake it for the product.

---

## 4 · Non-negotiables

Hard gates. Violating one is a defect regardless of whether tests pass.

**Provenance**

1. Every LLM-derived node and edge carries `evidence[]` — `{chunk_id, char_start, char_end, quote,
   extractor_version, prompt_hash, model, confidence}`. Not nullable, not deferred.
2. **The hallucination gate:** an extraction claim's `quote` must appear verbatim in the source
   chunk. If it does not, discard the claim. One string search catches most extraction errors.
3. Answers are assembled from retrieved evidence, never from model memory. The grounding validator
   rejects uncited assertions → retry once → return `insufficient_evidence`. A fluent guess is a
   defect, not a near-miss.
4. Extraction is versioned. Re-running supersedes; it never overwrites. "What did the graph say
   last Tuesday" must be answerable.

**Security**

5. ACL filtering happens **inside the SQL query**, never as a Python post-filter. Post-filtering
   leaks through result counts and latency.
6. A graph fact whose entire evidence set is ACL-invisible to the caller is invisible to the
   caller — filtered before it reaches the LLM, not after.
7. `org_id` on every row, every node, every Cypher template. No exceptions.
8. All connectors are read-only. No write scope requested in any OAuth flow, ever. This is a
   public promise on the landing page; code must make it structurally true.
9. No PII in logs, traces or error messages. Structured JSON with `trace_id`, `org_id`, opaque
   `user_id`.
10. Secrets from Secret Manager only. `.env.example` committed, `.env` never.

**Engineering**

11. No mock data behind any UI surface. Unfinished work is feature-flagged off, never faked. The
    demo runs live.
12. Every schema change is a migration — Alembic for Postgres, numbered Cypher for Neo4j.
13. Typed end to end: Pydantic v2 → OpenAPI → generated TypeScript client. Frontend types are
    never hand-written.
14. Ingestion is idempotent on `(org_id, source_system, external_id, content_hash)`.
15. `make preflight` — lint, typecheck, tests, migration drift — passes before any commit.

**Responsible AI**

16. Risk scores measure **knowledge concentration**, never an individual's probability of
    resigning. The deck's phrase "attrition-risk scoring" means org exposure if a role empties.
    Profiling named individuals' likelihood of leaving is a DPDP/GDPR problem and a demo-day
    question you do not want.
17. Any surface that ranks people must expose *why* — the evidence behind the ranking — to the
    person being ranked.
18. Individual-level risk drill-down requires explicit consent state on the Person node. Default
    views are aggregate.

---

## 5 · Stack (fixed — do not substitute)

```
Frontend   Next.js 16 App Router · TypeScript · Tailwind · shadcn/ui · TanStack Query   [A1]
Gateway    Python 3.12 · FastAPI · Pydantic v2 · SSE streaming
Agents     LangGraph (Postgres checkpointer) · LangChain for document loaders only
Graph      Neo4j 5 — AuraDB prod, CE container dev
Vector     PostgreSQL 16 + pgvector HNSW — Cloud SQL prod
LLM        Gemini via Vertex AI. Flash by default; Pro for extraction only.
Speech     AMI ships transcripts — use them. Chirp/Whisper only for live recording.
Queue      Cloud Tasks (prod) / arq + Redis (dev), behind one interface
Blobs      GCS · Container Docker multi-stage, distroless runtime
IaC        Terraform · CI GitHub Actions → Cloud Build → Artifact Registry → Cloud Run
```

**Deliberately absent:** Kafka, Kubernetes, service mesh, separate search cluster, Elasticsearch.
Four engineers, twelve weeks. Cloud Run plus a queue is enough. Anything more is a résumé, not an
architecture, and it will cost you the demo.

---

## 6 · Repository layout

```
jutsu/
├─ apps/
│  ├─ web/                Next.js — marketing routes + six product surfaces
│  ├─ api/                FastAPI gateway, stateless, request path only
│  └─ worker/             Ingestion, extraction, nightly batch agents
├─ packages/
│  ├─ core/               Pydantic domain models, enums, typed errors, PII, chunking, jobs
│  ├─ graph/              Neo4j driver, Cypher templates, migrations, temporal helpers
│  ├─ retrieval/          Embeddings, pgvector search, fusion, rerank, ACL filter
│  ├─ agents/             LangGraph graphs + versioned prompt files
│  ├─ connectors/         One module per source, all read-only
│  ├─ evals/              Gold sets, harness, CI runner, adversarial ACL suite
│  └─ ui/                 Shared React components + design tokens
├─ infra/{terraform,docker,cloudbuild}/
├─ data/corpora/          Enron + AMI loaders, synthetic Jira generator
├─ docs/{adr,runbook}/    plan-phase-N.md live here too
└─ Makefile               dev migrate seed test preflight eval gate deploy
```

Prompts live in `packages/agents/prompts/*.md`, load at runtime, and their hash is recorded on
every extraction claim. **A prompt change is a graph-affecting change** — treat it as one.
Package-specific rules go in nested `CLAUDE.md` files that load when work touches that directory.

---

## 7 · Data model — Neo4j

Nodes, all carrying `id`, `org_id`, `created_at`, `updated_at`, `source_ids[]`:

| Label | Key properties |
|---|---|
| `Person` | `email`, `display_name`, `title`, `dept`, `is_active`, `joined_at`, `left_at`, `consent_individual_view` |
| `Project` | `key`, `name`, `status`, `started_at`, `ended_at`, `criticality` |
| `Decision` | `statement`, `rationale`, `decided_at`, `status` ∈ {active, superseded, reversed}, `confidence`, `alternatives[]` |
| `Meeting` | `title`, `held_at`, `duration_s`, `platform` |
| `Document` | `title`, `source_system`, `source_uri`, `mime`, `created_at`, `modified_at`, `acl_hash` |
| `Message` | `thread_id`, `sent_at`, `channel`, `source_system` |
| `Topic` | `name`, `canonical_id`, `aliases[]` |
| `ActionItem` | `text`, `due_at`, `status` |
| `Ticket` | `key`, `status`, `created_at`, `resolved_at` |
| `Client` | `name` |
| `Team` | `name` |

Edges — every derived edge carries `valid_from`, `valid_to` (null = current), `recorded_at`,
`confidence`, `evidence[]`, `extractor_version`:

```
(Person)-[:WORKS_ON {role, contribution_score}]->(Project)
(Person)-[:AUTHORED]->(Document|Message)
(Person)-[:ATTENDED]->(Meeting)
(Person)-[:DECIDED]->(Decision)
(Person)-[:KNOWS {score, evidence_count, last_evidence_at}]->(Topic)
(Person)-[:REPORTS_TO]->(Person)
(Person)-[:ALIAS_OF]->(Person)                  -- reversible merge, never destructive
(Decision)-[:AFFECTS]->(Project)
(Decision)-[:SUPERSEDES]->(Decision)
(Decision)-[:EVIDENCED_BY]->(Document|Message|Meeting)
(Meeting)-[:PRODUCED]->(Decision|ActionItem)
(ActionItem)-[:OWNED_BY]->(Person)
(Project)-[:USES]->(Topic)
(Project)-[:FOR_CLIENT]->(Client)
(Document)-[:MENTIONS]->(Person|Project|Topic|Client)
```

**Bitemporality is mandatory and starts at migration 001.** Store valid time (when the fact was
true in the world) alongside transaction time (when JUTSU learned it). Superseding sets
`valid_to`; it never deletes. Write `packages/graph/temporal.py` with `supersede(tx, rel_id, at)`
and `as_of(tx, query, timestamp)` in week 2, before there is anything to store — retrofitting this
in week 8 means rewriting every edge and every template, and the decision ledger is worthless
without it.

Constraints and indexes in migration 001:

```cypher
CREATE CONSTRAINT person_org_email IF NOT EXISTS
  FOR (p:Person) REQUIRE (p.org_id, p.email) IS UNIQUE;
CREATE CONSTRAINT doc_source_id IF NOT EXISTS
  FOR (d:Document) REQUIRE (d.org_id, d.source_system, d.external_id) IS UNIQUE;
CREATE CONSTRAINT project_key IF NOT EXISTS
  FOR (pr:Project) REQUIRE (pr.org_id, pr.key) IS UNIQUE;
CREATE INDEX decision_time IF NOT EXISTS FOR (d:Decision) ON (d.org_id, d.decided_at);
CREATE FULLTEXT INDEX topic_name IF NOT EXISTS FOR (t:Topic) ON EACH [t.name];
CREATE FULLTEXT INDEX decision_text IF NOT EXISTS FOR (d:Decision) ON EACH [d.statement];
```

---

## 8 · Data model — PostgreSQL

```sql
CREATE EXTENSION IF NOT EXISTS vector;

orgs             (id uuid pk, name, created_at)
users            (id uuid pk, org_id fk, external_id, email, display_name, is_active)
user_groups      (user_id, group_external_id)                    -- IdP-synced, pk both
sources          (id uuid pk, org_id fk, system, config_json, last_sync_cursor,
                  last_sync_at, status)
documents        (id uuid pk, org_id fk, source_id fk, external_id, uri, title, mime,
                  author_external_id, content_hash, acl_hash, body_original text,
                  body_masked text, created_at, modified_at, ingested_at, superseded_by uuid)
chunks           (id uuid pk, document_id fk, ordinal int, text, char_start int,
                  char_end int, token_count int, embedding vector(768))
document_acl     (document_id fk, principal_type, principal_id, permission)
pii_vault        (vault_key pk, document_id fk, pii_type, ciphertext bytea)
extraction_runs  (id uuid pk, org_id, extractor_version, prompt_hash, model,
                  started_at, finished_at, stats_json)
extraction_claims(id uuid pk, run_id fk, chunk_id fk, claim_type, payload_json,
                  confidence, superseded_by uuid)
resolution_queue (id uuid pk, org_id, entity_kind, payload_json, candidates_json,
                  score, state)                                   -- human review below threshold
jobs             (id uuid pk, org_id, kind, state, idempotency_key unique,
                  payload_json, attempts int, locked_until, error, created_at)
audit_log        (id bigserial pk, org_id, actor_id, action, resource_type,
                  resource_id, ts, meta_json)
eval_runs, eval_results
```

```sql
CREATE UNIQUE INDEX ON documents (org_id, source_id, external_id);
CREATE INDEX ON documents (content_hash);
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
CREATE INDEX ON chunks (document_id, ordinal);
CREATE INDEX ON document_acl (principal_id, document_id);
```

Row-level security on `documents`, `chunks`, `document_acl` keyed on `org_id` — enabled **and
forced**. LangGraph checkpoints get their own schema, never mixed with domain tables. Raw source
blobs live in GCS; Postgres holds text and metadata only.

---

## 9 · Ingestion

```
fetch → normalise → ACL capture → PII mask → chunk → embed → extract → resolve → merge → audit
```

Each stage is a separately queued job with its own retry policy and idempotency key. A failure at
`extract` must not force a re-`fetch`.

**Connector protocol** — every source implements exactly this, read-only:

```python
class SourceSystem(StrEnum):
    LOCAL = "local"
    GMAIL = "gmail"
    M365 = "m365"
    SLACK = "slack"
    JIRA = "jira"
    CONFLUENCE = "confluence"
    GITHUB = "github"


class AclEntry(BaseModel):
    principal_type: Literal["user", "group", "org", "public"]
    principal_id: str
    permission: Literal["read"] = "read"


class RawDocument(BaseModel):
    external_id: str
    source_system: SourceSystem
    uri: str | None
    title: str
    body: str
    mime: str = "text/plain"
    author_external_id: str | None
    participant_external_ids: list[str] = []
    thread_id: str | None = None
    created_at: datetime
    modified_at: datetime | None = None
    acls: list[AclEntry]
    raw_metadata: dict[str, Any] = {}

    @computed_field
    @property
    def content_hash(self) -> str:
        return sha256(self.body.encode()).hexdigest()


class Connector(Protocol):
    system: SourceSystem

    async def list_since(self, cursor: str | None) -> AsyncIterator[str]: ...
    async def fetch(self, external_id: str) -> RawDocument: ...
    async def acls(self, external_id: str) -> list[AclEntry]: ...
```

### 9.1 PII masking with an offset map

The hardest correctness problem in the ingestion path. Masking changes string length; citations
must highlight spans in the **original** text; the LLM must only ever see the **masked** text.

```python
class PiiType(StrEnum):
    PERSON = "person"
    EMAIL = "email"
    PHONE = "phone"
    ADDRESS = "address"
    GOV_ID = "gov_id"
    FINANCIAL = "financial"


class MaskedSpan(BaseModel):
    pii_type: PiiType
    token: str  # "[PERSON_A7]" — stable within a document
    orig_start: int  # CHARACTER offsets, not bytes
    orig_end: int
    masked_start: int
    masked_end: int
    vault_key: str


class MaskResult(BaseModel):
    masked_text: str
    spans: list[MaskedSpan]  # sorted by orig_start, non-overlapping

    def to_original(self, masked_offset: int) -> int: ...
    def to_masked(self, orig_offset: int) -> int: ...


def mask(text: str, detectors: Sequence[PiiDetector]) -> MaskResult: ...
```

Required tests, written before the implementation: no-PII identity case; replacement shorter than
original; replacement longer; two adjacent replacements; PII at index 0; PII touching the end;
same entity three times → same token, three spans; round trip `to_masked(to_original(i)) == i`
outside spans; **non-ASCII text (Devanagari, emoji) → character indices not byte indices**;
overlapping detector hits → longest match wins, no overlapping result spans.

Detectors run in a fixed order and merge deterministically. Masking must be reproducible, because
`content_hash` and every chunk offset depend on it. Originals go to `pii_vault` encrypted with an
org-scoped Secret Manager key; rehydrate on display only, for callers who pass the ACL check.

### 9.2 Chunking

```python
class Chunk(BaseModel):
    ordinal: int
    text: str  # MASKED text — what gets embedded and shown to the LLM
    char_start: int  # offset into the ORIGINAL document body
    char_end: int
    token_count: int


def chunk_document(
    masked: MaskResult, target_tokens: int = 768, overlap_ratio: float = 0.15, min_tokens: int = 128
) -> list[Chunk]: ...
```

Split on headings → paragraph breaks → sentence breaks → hard token limit. Never split inside a
`MaskedSpan` or across a decision statement. Convert boundary offsets through `to_original()`
before storing — the citation UI highlights these exact spans, so an off-by-one is a visible
product bug, not a rounding error.

### 9.3 Embedding

```python
async def embed_batch(
    texts: Sequence[str], task_type: Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]
) -> list[list[float]]:
    """Gemini text-embedding-004, 768 dims. Batches of 100, exponential backoff,
    token accounting to extraction_runs.stats_json."""
```

Query and document embeddings use *different* `task_type` values. Using one for both silently
costs several points of recall and stays invisible until eval — assert the distinction in a test.

---

## 10 · Extraction

```python
class Evidence(BaseModel):
    chunk_id: UUID
    char_start: int
    char_end: int
    quote: str  # verbatim; MUST appear in the chunk or the claim is discarded
    extractor_version: str
    prompt_hash: str
    model: str
    confidence: float


class ExtractedEntity(BaseModel):
    local_ref: str  # unique within this extraction batch
    kind: Literal[
        "person", "project", "decision", "meeting", "topic", "action_item", "client", "ticket"
    ]
    payload: dict[str, Any]
    evidence: Evidence


class ExtractedRelation(BaseModel):
    type: RelType
    source_ref: str
    target_ref: str
    props: dict[str, Any] = {}
    valid_from: datetime | None = None
    evidence: Evidence


class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]
```

Gemini returns strict JSON conforming to this schema. The hallucination gate runs before anything
touches the graph:

```python
def gate(result: ExtractionResult, chunk_text: str) -> ExtractionResult:
    """Drop every claim whose evidence.quote is not literally present in chunk_text."""
```

**Decision extraction is the differentiator** and deserves its own schema and prompt:

```python
class DecisionClaim(BaseModel):
    statement: str  # "We will use PostgreSQL for the primary store"
    rationale: str | None  # why
    decided_at: datetime
    decider_refs: list[str]
    alternatives_considered: list[str]  # "MongoDB", "DynamoDB"
    affects_project_refs: list[str]
    supersedes_hint: str | None  # free text, resolved in a second pass
    evidence: Evidence
```

Distinguish a decision from a proposal, an opinion and a restatement — the prompt must give
positive and negative examples of each. A ledger full of "someone once mentioned Postgres" is
worse than an empty one, because it looks authoritative.

---

## 11 · Entity resolution

The sharpest technical risk in the project, and the week-4-to-6 slip that kills these builds.
Make it explicit; never let the LLM do it implicitly.

```python
class ResolutionDecision(BaseModel):
    action: Literal["merge", "create", "review"]
    target_id: UUID | None
    score: float
    signals: dict[str, float]  # per-signal contribution, for audit


def resolve_person(
    candidate: ExtractedEntity, existing: Sequence[PersonRecord], thresholds: ResolutionThresholds
) -> ResolutionDecision: ...
```

Pipeline: **block** on normalised email, display name and handle → **score** on exact email (1.0,
deterministic merge), name similarity, co-occurrence in threads and projects, temporal overlap →
**threshold**: ≥0.92 auto-merge, 0.65–0.92 into `resolution_queue` for human review, <0.65 create
new.

Merges are reversible and audited. Never destructively merge two `Person` nodes — write an
`ALIAS_OF` edge and resolve at query time. You will get merges wrong, and an irreversible wrong
merge silently corrupts every downstream score.

---

## 12 · Retrieval — GraphRAG

```
question
  → intent classify {factual | decision_trace | expert | timeline | handover | onboarding}
  → entity extract + resolve to graph IDs
  → parallel:
       (a) Cypher: parameterised template selected by intent
       (b) pgvector: top-k=30, ACL-filtered
  → graph expansion: 1-hop neighbours of vector hits
  → fusion: reciprocal rank fusion (k=60), then cross-encoder rerank to top-8
  → synthesis: answer with mandatory inline citation markers
  → validate grounding → retry once → else insufficient_evidence
  → {answer, citations[], graph_path[], confidence, latency_ms}
```

```python
class Citation(BaseModel):
    marker: int  # [1], [2] in the answer markdown
    chunk_id: UUID
    document_id: UUID
    document_title: str
    source_system: SourceSystem
    char_start: int  # into the ORIGINAL document, for highlighting
    char_end: int
    occurred_at: datetime


class Answer(BaseModel):
    markdown: str
    citations: list[Citation]
    graph_path: list[GraphHop]
    confidence: float
    latency_ms: int
    insufficient_evidence: bool = False


def validate_grounding(markdown: str, citations: list[Citation]) -> GroundingReport:
    """Split into atomic claims; each must carry ≥1 marker. Returns uncited_claims[]."""
```

The ACL filter lives in the SQL. Not in Python. Not after the fetch:

```sql
SET LOCAL hnsw.ef_search = 100;

SELECT c.id, c.document_id, c.text, c.char_start, c.char_end,
       1 - (c.embedding <=> $1::vector) AS score
FROM chunks c
JOIN documents d ON d.id = c.document_id
WHERE d.org_id = $2
  AND d.superseded_by IS NULL
  AND EXISTS (
    SELECT 1 FROM document_acl a
    WHERE a.document_id = d.id AND a.permission = 'read'
      AND (   (a.principal_type = 'user'  AND a.principal_id = $3)
           OR (a.principal_type = 'group' AND a.principal_id = ANY($4))
           OR (a.principal_type = 'org'   AND a.principal_id = $2::text))
  )
ORDER BY c.embedding <=> $1::vector
LIMIT $5;
```

A restrictive filter makes HNSW return fewer than `k`. Raise `ef_search` and over-fetch. Do not
"fix" a short result set by loosening the filter.

**On text-to-Cypher:** parameterised templates keyed to intent are the default path. If you add
generated Cypher, it runs as a read-only Neo4j user with a statement timeout, a label and
relationship allowlist, and rejection of anything matching `CREATE|MERGE|DELETE|SET|CALL db.`. An
LLM writing arbitrary queries against a company's memory is not a feature.

---

## 13 · Agent layer

The pitch says six agents. Build **four graphs**; the six capabilities are surfaces over them.
Six near-identical LangGraph state machines to match a slide is two wasted weeks.

1. **`extraction`** — offline worker. Chunk → entities/relations/decisions → gate → resolve →
   merge. Checkpointed, resumable, batched.
2. **`qa`** — online, §12 pipeline. Serves Cited Q&A, Decision Ledger and Onboarding Copilot via
   different intents and prompts on one graph.
3. **`analysis`** — nightly batch. Recomputes contribution scores, `KNOWS` weights, bus-factor and
   centrality. Writes derived properties with `computed_at`.
4. **`composer`** — on-demand async job. Walks a subgraph and drafts long-form output. Serves
   Handover Packs and Project Historian timelines. Returns a job ID; the UI polls.

Shared state is a typed Pydantic model. Every node logs inputs, outputs, token cost and latency to
the trace. **Token budget per request is a hard ceiling enforced in code** — an agent that loops
is a billing incident, and you are on student tiers.

---

## 14 · Scoring

Implement exactly; tune constants in config, not code.

**Contribution** — `score(person, project) = Σ_e w(type_e) · exp(-λ · age_e)`
with `w`: decision authored 5.0, document authored 3.0, ticket resolved 2.0, meeting attended 1.0,
mention 0.5. Half-life 180 days.

**Expertise** — same shape over `Topic` edges, normalised per topic so scores compare across
topics. This is what makes Expert Discovery rank on real work rather than CV claims.

**Bus factor** — sort contributors by score descending; bus factor is the smallest *n* whose
cumulative share ≥ 50%. Escalate when `n == 1` **and** that person solely owns open `ActionItem`s
or is sole author of active decisions.

**Risk** — 0–100 composite of inverse bus factor, criticality (project status + client link), and
staleness of the second-most-knowledgeable person's last contribution. Aggregate-first display.

Every score exposes its `signals` breakdown. A ranking you cannot explain is a ranking you cannot
defend to the person being ranked, or to a judge asking how it works.

---

## 15 · API surface

```
POST   /v1/ask                          SSE stream; {question, filters}
GET    /v1/decisions?project=&q=&as_of= ledger, filterable, temporal
GET    /v1/decisions/{id}/trace         full provenance chain
GET    /v1/projects/{id}/timeline
GET    /v1/experts?topic=&limit=
GET    /v1/risk/overview                aggregate, always allowed
GET    /v1/risk/projects/{id}
POST   /v1/handover/{person_id}         → 202 + job_id
GET    /v1/jobs/{id}
POST   /v1/onboarding/path              {person_id, project_id}
GET    /v1/graph/subgraph?node=&depth=
GET    /v1/evidence/{chunk_id}          source span + highlight offsets
POST   /v1/connectors/{id}/sync
GET    /healthz  /readyz  /metrics
```

Every response carries `request_id`. One typed error envelope for all 4xx/5xx. Rate limits per org
and per user. `/v1/ask` streams tokens **and** streams citations as they resolve — sources should
appear before the sentence finishes.

---

## 16 · Frontend

### Design system — inherited from the existing landing page

> **Amended (A2).** The authoritative tokens are those actually shipped on the landing page, not
> the values originally listed here. See `apps/web/app/globals.css` and `packages/ui/tokens.ts`.

```
--brand          green sampled from the logo — #83d005 bright / #499f02 deep
                 dark theme: bright green on obsidian
                 light theme: deep green on off-white
                 --brand and --graph FLIP lightness role per theme; --brand-foreground
                 inverts to match, so one set of utilities works in both
--graph          low-chroma teal, the secondary — held down so green stays dominant
--foreground     near-white on dark, near-black on light
--background     obsidian #0a0b0f dark / off-white #f9fafb light
--hairline       borders, alpha-based so they work on either ground
Type             Geist sans, tight tracking on display sizes
Wordmark         vector-traced from the supplied artwork — lib/wordmark-paths.ts, never a font
Micro-labels     monospace uppercase with a leading bullet — · THE PROBLEM, · ARCHITECTURE
                 This is the signature UI accent. Carry it into the product surfaces.
Stat blocks      oversized green figure, mono caption, small source attribution line
Motion           restrained — reveal on scroll, no bounce, no confetti
                 every scroll-driven animation collapses under prefers-reduced-motion
```

Do not introduce a second design system. A product that looks unrelated to its own landing page
reads as unfinished, and this is the first thing a judge sees.

### Route structure

```
apps/web/src/app/
  (marketing)/   existing landing page — /, problem, architecture, comparison, faq
  (product)/     ask/ decisions/ experts/ risk/ handover/ onboarding/
  layout.tsx
middleware.ts    unauthenticated → (marketing); authenticated → (product)
```

Move the existing app into `apps/web` unchanged, verify it still builds, *then* split into route
groups. Lift design tokens into `packages/ui/tokens.ts` and import into both.

### Surface requirements

- **Citation UX is the product.** Clicking a citation opens a side panel with the source document,
  the exact span highlighted, source system and date. If this feels cheap, the product feels cheap.
- **Graph explorer** — force-directed canvas (`react-force-graph` or Cytoscape), progressive
  loading, max 300 rendered nodes with expand-on-demand. The landing page's live graph animation
  sets the expectation; meet it.
- **Streaming answers** via SSE with skeleton states. Never a spinner over a blank page.
- **Decision ledger** — filterable timeline with an `as_of` control. Superseded decisions render
  struck through with a link to what replaced them. This is the moat; make it look like it.
- **Risk dashboard** defaults to aggregate. Individual drill-down shows a consent banner.
- **Handover Studio** — draft as an editable document with inline citations, export to PDF and
  Markdown.
- Accessibility: keyboard-navigable graph, WCAG AA contrast, `prefers-reduced-motion` honoured.

---

## 17 · Security and compliance

- **AuthN** — Google Identity Platform, OAuth2 + SAML for enterprise IdPs. httpOnly cookie
  sessions, refresh rotation.
- **AuthZ** — RBAC roles (viewer, member, lead, admin) *plus* source-ACL inheritance. Roles gate
  features; ACLs gate data. Never conflate them.
- **Multi-tenancy** — `org_id` on every row and node, RLS in Postgres, a lint rule failing CI on
  any Cypher template lacking `org_id`.
- **Crypto** — AES-256 at rest (CMEK on Cloud SQL and GCS), TLS 1.3 in transit.
- **Audit** — every person-level risk read, every handover generation, every ACL change → immutable
  `audit_log`, exportable.
- **DPDP Act 2023 / GDPR** — data subject export and erasure endpoints. Erasure cascades to chunks,
  embeddings and evidence, and marks derived claims orphaned rather than leaving dangling
  provenance. DPIA in `docs/` before any live-data pilot.

**Adversarial ACL suite** — in CI, any failure blocks release:

1. No grants → zero results, not an error.
2. Direct user grant → exactly that document's chunks.
3. Group grant → group documents; removing membership removes them on the next query, no cache
   flush.
4. Org-wide grant behaves as expected.
5. Cross-org isolation — a user in org A whose `principal_id` matches a grant in org B sees nothing.
6. Result *counts* do not leak; two users with disjoint grants get independent counts.
7. `EXPLAIN` output contains the `document_acl` join — this is the test that catches a future
   refactor quietly moving the filter into Python.

---

## 18 · Evaluation

Non-optional. Subset per PR, full suite nightly, tracked per milestone.

| Dimension | Target | Method |
|---|---|---|
| Entity/relation extraction | precision ≥85%, recall ≥80% | 200-doc labelled gold set, per-type F1 |
| Retrieval | recall@10 ≥90% | 100 questions with known evidence documents |
| Groundedness | ≥95% cited, **zero** uncited claims | automated claim splitter + citation check |
| Answer quality | ≥80% acceptable | LLM judge + 20% human spot check |
| Latency | p95 < 3s end to end | load test at 20 concurrent |
| ACL integrity | 100% | adversarial suite; any failure blocks release |
| Entity resolution | ≥95% precision on merges | held-out labelled pairs |
| Cost | under configured ceiling per 1k questions | token accounting per request |

Gold sets versioned in git. When a metric regresses, CI fails and names the commit. "It felt
better" is not a result, and neither is a number you did not measure.

---

## 19 · Pilot corpora

- **Enron email** (~500k messages, public). Sample 40–60k. **Sample complete threads, never random
  messages** — random sampling shreds the reply graph and leaves entity resolution with nothing to
  resolve, which you will not notice until week 5.

```python
def sample_enron(root: Path, seed: int = 20260819, target_messages: int = 50_000,
                 custodian_count: int = 150) -> Iterator[RawDocument]:
    """Select custodians by mailbox size, then take COMPLETE threads involving them
    until target_messages. Threads are never truncated. Emit sample_manifest.json;
    the same seed must produce a byte-identical manifest."""
```

- **AMI Meeting Corpus** (~100h) — use the shipped transcripts. Whisper/Chirp is the
  live-recording path only, and it is not on the critical path for the demo.
- **Synthetic Jira/Confluence layer** — seeded generator, tickets and projects assigned to real
  Enron personas so the graph joins across sources. ~12 projects, 400 tickets, 60 pages.

Demo runs on public and synthetic data only. No live employee data before a signed DPIA. Say this
on screen during the demo — it is a point in your favour, not a caveat.

---

## 20 · GCP deployment

```
Cloud Run          web / api / worker (worker min-instances=1, others 0→N)
Cloud SQL          PostgreSQL 16, pgvector, private IP, CMEK
Neo4j AuraDB       managed — fallback GCE e2-standard-4 + Neo4j CE + persistent disk
Cloud Tasks        ingestion + extraction queues
GCS                raw blobs, lifecycle to Nearline at 30 days
Secret Manager     all credentials
Artifact Registry  with a cleanup policy from the first push
Cloud Build        on main; deploy behind a revision tag before shifting traffic
VPC connector      Cloud Run → Cloud SQL private IP
Vertex AI          Gemini, regional
Observability      Cloud Logging/Monitoring/Trace + OpenTelemetry from FastAPI and LangGraph
```

**Cost guardrails — day one, not after the first bill:**

- Budget alerts at 50/80/100% of a stated monthly ceiling, to two people.
- `min-instances=0` on web and api; concurrency 80 for web, 8 for api given LLM calls.
- Artifact Registry cleanup policy from the first push. Untagged image layers are the classic
  silent GCP bill.
- Token budget ceiling per request in code; nightly batch on Flash tier with caching and batching.
- One `terraform destroy`-able dev environment, torn down outside working hours.

Environments: `dev` (local Compose), `staging` (GCP, synthetic data), `prod` (GCP, pilot data).
Staging and prod differ in data and scale only, never in configuration shape.

---

## 21 · Twelve-week build plan

Each phase ends at a gate. Do not start the next phase until the gate passes — carrying a broken
foundation forward is how twelve-week projects become eighteen. Lanes: **D** data, **B**
graph/backend, **A** AI/RAG, **F** frontend.

### Phase 1 · Weeks 1–3 — Foundation & Ingestion

| Slice | Lane | Content |
|---|---|---|
| S0 | F | Monorepo (pnpm + uv workspaces), Compose, CI, landing page folded into `apps/web`, product routes stubbed behind auth |
| S1 | B | Postgres migration 001 + pgvector + RLS (§8) |
| S2 | B | Neo4j migration runner + constraints + `temporal.py` (§7) |
| S3 | D | Corpus loaders, connector protocol, Enron thread sampler (§19) |
| S4 | D | PII masking + offset map, ten tests (§9.1) |
| S5 | D | Chunker with original-document offsets (§9.2) |
| S6 | A | Embedder + HNSW (§9.3) |
| S7 | B | ACL capture + filtered search + 7 adversarial tests (§12, §17) |
| S8 | B | Job queue, idempotent pipeline, crash resume |
| S9 | A | Gate harness `scripts/gate.py --phase 1` |

S1–S3 parallel after S0. S4 → S5 → S6 → S7 is the critical path; protect it.

**Gate M1:** ≥45k documents ingested; second `make seed` adds zero rows; every chunk offset
resolves to matching original text; zero raw PII in captured logs; 100% chunks embedded at dim
768; all 7 ACL tests pass; org isolation proven; migrations reversible; preflight green with ≥70%
coverage on core/graph/retrieval; seed-run token cost recorded.

### Phase 2 · Weeks 4–6 — Knowledge Graph

| Slice | Lane | Content |
|---|---|---|
| S10 | A | Extraction schemas + prompts + hallucination gate (§10) |
| S11 | A | Decision extraction — its own prompt, positive/negative examples |
| S12 | B | Entity resolution: blocking, scoring, thresholds, review queue (§11) |
| S13 | B | Graph merge with bitemporal edges + evidence, supersession |
| S14 | D | 200-doc gold set labelled, extraction eval harness |
| S15 | F | Graph explorer UI on real data |

**Gate M2:** browsable graph of people, projects and decisions from real corpus data; extraction
precision ≥80% on the gold set; every edge carrying evidence; resolution precision ≥95% on
held-out pairs; `as_of()` returns pre-supersession state.

### Phase 3 · Weeks 7–9 — GraphRAG Intelligence

| Slice | Lane | Content |
|---|---|---|
| S16 | A | Intent classifier + entity resolution to graph IDs |
| S17 | A | Dual retrieval, RRF fusion, rerank |
| S18 | A | Answer synthesis + grounding validator + refusal path (§12) |
| S19 | F | Cited Q&A UI with source panel and span highlighting |
| S20 | F | Decision ledger UI with `as_of` control and supersession rendering |
| S21 | B | Expert finder endpoint + scoring (§14) |
| S22 | B | Project historian timelines |
| S23 | A | 100-question benchmark + groundedness eval |

**Gate M3:** ≥90% groundedness on the benchmark; zero uncited claims; p95 < 4s; ACL adversarial
suite green; citation panel highlights correct spans on twenty hand-checked answers.

### Phase 4 · Weeks 10–12 — Risk, Handover, Hardening

| Slice | Lane | Content |
|---|---|---|
| S24 | B | Analysis agent — contribution, expertise, bus factor, centrality, nightly |
| S25 | F | Risk dashboard, aggregate-first, consent-gated drill-down |
| S26 | A | Composer agent — handover pack schema, subgraph walk, drafting |
| S27 | F | Handover Studio — editable, cited, PDF/Markdown export |
| S28 | A | Onboarding copilot — ordered reading path |
| S29 | B | Terraform, Cloud Run deploy, budget alerts, load test |
| S30 | all | Runbook, DPIA, demo rehearsal, documentation |

**Gate M4:** full five-minute demo live on GCP; all §18 targets met; documentation complete;
`terraform destroy` and re-apply works.

**Locked MVP fallback, decided end of week 9:** graph + cited Q&A + risk score. Onboarding copilot
and handover packs defer gracefully. Ship the moat, not the perimeter.

---

## 22 · Working agreement

1. **Read the phase spec first.** `docs/plan-phase-N.md` defines slices, contracts and gates. Read
   the whole slice before writing any file.
2. **One slice per session.** Slices fit a single context window. Start slice N+1 in a fresh
   session so the spec is re-read rather than half-remembered. Never roll into a new slice on a
   compacted context — that is where `evidence[]` starts going missing.
3. **Plan mode before code.** Propose files and interfaces; wait for approval; then build.
4. **Vertical, never horizontal.** "Ingest one document → see it in the graph → ask one question
   about it" beats three weeks of scaffolding with nothing to look at.
5. **Tests with the code.** Unit tests for scoring, resolution and offset logic; contract tests for
   connectors; one e2e per surface. Coverage ≥70% on core, graph, retrieval.
6. **ADR every real decision.** Context, options, choice, consequences. If you explain a choice
   twice in chat, it should have been an ADR.
7. **No TODOs in merged code.** Implement it or open an issue and reference the number.
8. **Never invent numbers.** Metrics come from `make eval`, latency from traces, cost from token
   accounting. If you do not have the measurement, say so.
9. **Surface uncertainty immediately.** If a requirement is ambiguous or a target looks
   unachievable, say so with the specific reason before building around it. Silent scope
   reinterpretation is the most expensive thing you can do here.

**Known traps in this codebase** — keep these in `CLAUDE.md`:

- Chunk offsets are against the original text, not the masked text. `to_original()` is the only
  correct translation path.
- HNSW plus a restrictive ACL filter returns fewer than `k`. Raise `ef_search`; do not loosen the
  filter.
- Random-sampling Enron destroys the reply graph. Sample complete threads.
- Entity merges must be reversible. Never destructively merge two Person nodes.
- LLM-generated Cypher runs read-only, with a timeout and a label allowlist.

---

## 23 · The demo — five minutes, end to end

Scripted, live, on the pilot corpus. No mock screens.

| Time | Beat |
|---|---|
| 0:00–0:45 | **Ingest** — documents, emails, meeting transcripts, Jira, Confluence. Corpus imported, memory graph builds on screen. |
| 0:45–1:30 | **Recover a decision** — "Why did we choose PostgreSQL over MongoDB?" Exact decision, meeting context, owner, evidence, citations. |
| 1:30–2:15 | **Discover an expert** — "Who has the strongest Kubernetes cost-optimisation expertise?" Ranked on contributions, not CV claims. |
| 2:15–3:15 | **Identify risk** — single points of failure, quantified exposure. "Priya is sole owner on 3 critical projects (bus factor 1)." |
| 3:15–4:15 | **Generate a handover pack** — open actions, key decisions, stakeholders, project history, known risks. Fully cited, under 60 seconds. |
| 4:15–5:00 | **Ramp a new joiner** — "What should I read first for Project Falcon?" Personalised path from organisational memory. |

Close on the line the whole system exists to earn: **Ask anything. Trace any decision. Lose
nothing.**

---

## 24 · Definition of done

- [ ] Six surfaces live on GCP — no mock data, no placeholder copy
- [ ] Every answer carries working citations to highlighted source spans
- [ ] Decision ledger answers "why did we choose X" with date, owner, meeting and supersession chain
- [ ] Bus-factor risk computed nightly, visible as an aggregate dashboard, explainable per score
- [ ] Handover pack generates in under 60 seconds, fully cited, exportable
- [ ] All §18 eval targets met and reproducible via `make eval`
- [ ] ACL adversarial suite green across all six surfaces
- [ ] Terraform provisions the full stack from zero; destroy and re-apply verified
- [ ] Budget alerts configured and proven to fire in a test
- [ ] Runbook covers deploy, rollback, corpus re-ingest, key rotation, incident response
- [ ] DPIA written; demo runs on public and synthetic data only
- [ ] Five-minute demo rehearsed and reproducible on demand
