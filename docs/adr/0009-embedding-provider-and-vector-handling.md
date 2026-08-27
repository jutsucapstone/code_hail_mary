# ADR 0009 — `gemini-embedding-001` at 768, normalised here, truncation refused

- **Status:** accepted
- **Date:** 2026-08-27
- **Slice:** S6

## Context

§9.3 specifies "Gemini text-embedding-004, 768 dims. Batches of 100, exponential backoff,
token accounting". Three of those four turned out to need revisiting once the API was
actually called, and the fourth — 768 — turned out to be the constraint everything else
had to fit around, because `chunks.embedding` is `vector(768)` with an HNSW index already
built on it.

**Every number below was measured against `asia-south1` on 2026-08-27**, not read from
documentation. Roughly twenty-two requests and fifteen thousand input tokens, a fraction
of one paisa.

## Decision 1 — `gemini-embedding-001` with `outputDimensionality=768`

Verified callable in `asia-south1`, along with `text-embedding-004` and
`text-multilingual-embedding-002`.

Selected on criteria in this order, none of them convenience:

| Criterion | Why it decides |
|---|---|
| Emits 768 | The column and its HNSW index already exist. Anything else is a migration and a full re-index. |
| Multilingual | The corpus is not ASCII-only and S4/S5 are tested on Devanagari and emoji. `text-embedding-005` is English-only and disqualified on this line alone. |
| `task_type` support | §9.3's named correctness hazard. |
| Regional availability | §20 makes Vertex regional here; the region is a data-residency decision. |
| Not on a retirement path | Re-embedding a corpus is a full re-index, so the older generation is a liability. |

`text-embedding-004` remains the documented fallback: native 768, no truncation step.

## Decision 2 — vectors are L2-normalised in this package

Measured:

| `outputDimensionality` | dims | L2 norm |
|---|---|---|
| default | 3072 | **1.000000** |
| **768** | 768 | **0.583809** |

**MRL-truncated output is not normalised.** Cosine distance is scale-invariant, so
`vector_cosine_ops` would rank correctly either way and this is not a correctness fix
today. It is normalised anyway because the ops class is a schema decision that may change
— an inner-product index over mixed-magnitude vectors is silently wrong — and because a
store where some vectors are unit-length and others are not is a trap for whoever changes
it.

The recorded test fixture is a **real provider response** with norms of 0.583809 and
0.584159. A synthetic fixture would have been written already normalised and every
normalisation assertion would have passed against nothing.

## Decision 3 — `truncated=true` is a hard failure

Measured: 1201 tokens `truncated=False`, 1761 `truncated=False`, **2081 `truncated=True`
under HTTP 200** with a well-formed vector. The limit is 2048.

Silent truncation is the worst failure shape available here. The status says success, the
vector is the right width, and it describes a prefix of the chunk — so retrieval degrades
in a way that is invisible until eval, which is precisely what §9.3 warns about for
`task_type`. `Embedder` raises `TruncatedInput` carrying the global index, and nothing
truncated is ever returned, let alone persisted.

At the default 768-token chunk target this is unreachable — even at the estimator's worst
observed under-count the real cost lands near 1024, half the limit. It is enforced because
the headroom is what makes it safe, and headroom is a thing people change.

## Decision 4 — batch 250, configurable, bounded twice

§9.3 says 100. Measured:

| instances | 1 | 2 | 5 | 10 | 50 | 250 |
|---|---|---|---|---|---|---|
| latency | ~0.7 s | 0.72 s | 1.01 s | 1.39 s | 3.30 s | **16.8 s** |

250 succeeded. The deviation is deliberate, and the reason is that **requests per minute
is the binding constraint, not instances per request**: a 429 on
`online_prediction_requests_per_base_model` arrived after roughly eight rapid requests,
long before any batch ceiling. Fewer, larger requests is therefore strictly better, and
concurrency defaults to 2 because more of it buys throughput only until the quota trips,
at which point everything in flight fails together.

**250 is a measured default, not a provider guarantee.** It is configurable, and it is
bounded a second time by a per-request token budget — 250 chunks of 768 tokens is not a
request anyone should send, whatever the instance cap happens to be.

An earlier reading of the same evidence was wrong and is recorded here because the
mistake is instructive: the first probe failed at ten instances and looked like a batch
limit. It was the per-minute quota. Spacing the requests showed 250 working.

## Decision 5 — error classification is the retry policy

Measured: unknown `task_type` → 400 `INVALID_ARGUMENT`; empty content → 400
`INVALID_ARGUMENT`; too many requests → 429.

- **429, 5xx, timeouts, connection failures** → retry with exponential backoff and *full*
  jitter. Unjittered backoff retries every concurrent batch at the same instant and trips
  the per-minute quota again.
- **400 and other 4xx** → never retried. The request will be rejected identically every
  time, and on a corpus-sized job retrying it spends real quota to receive the same answer.

## Decision 6 — the provider's token count is authoritative

`statistics.token_count` is what was billed. It is written to `chunks.token_count`,
replacing S5's estimate, and it is what the budget ledger charges against. ADR 0006 said
the real number would arrive here; it has.

## Consequences

- **No migration.** `vector(768)`, HNSW `m=16, ef_construction=64`, `vector_cosine_ops`,
  pgvector 0.8.6 — all verified live and all unchanged.
- **A model change is a full re-embed and re-index**, which under §4.4 is a supersede.
- **Resumability is the selection**, not a checkpoint: pending work is `WHERE embedding IS
  NULL`, so a killed worker resumes by asking the same question and a completed run is a
  no-op.
- `google-auth` and `httpx`, not `google-cloud-aiplatform`. The SDK is a very large
  dependency tree for one authenticated POST.
- **The batch limit and the quota are observations about one model, one region, one day.**
  They are configuration, and the numbers in `config.py` each record what was measured.

## A correction to ADR 0006

ADR 0006 claimed `estimate_tokens` is "deliberately conservative — it over-counts rather
than under-counts". **Measured against the real tokenizer, that is false:**

| sample | estimate | actual | ratio |
|---|---|---|---|
| ascii prose | 43 | 30 | 1.43 |
| **masked tokens** `[EMAIL_A7]` | 21 | 28 | **0.75** |
| devanagari | 69 | 32 | 2.16 |
| emoji mix | 19 | 14 | 1.36 |
| **quoted reply** | 20 | 21 | **0.95** |

It under-counts worst on **masked text**, which is exactly what gets embedded — bracketed
pseudonyms tokenize into more tokens than four-characters-per-token predicts.

The claim is corrected in ADR 0006 and in the docstring. The behaviour is **not** changed,
and the reasoning is worth stating: at the default 768-token target the worst observed
ratio puts real usage near 1024 against a 2048 limit, so the headroom absorbs the error
completely. What made this safe was the headroom, not the conservatism — and the docstring
previously invited someone to raise `target_tokens` toward the limit on the strength of a
guarantee that does not hold. `truncated=true` rejection is the backstop that makes the
remaining risk visible rather than silent.
