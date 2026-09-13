# ADR 0024 — One three-provider LLM stack for answers and extraction

**Status:** accepted
**Date:** 2026-09-13
**Supersedes:** ADR 0023 (multi-provider answer failover) — its seam, its taxonomy and its
budget stand; its choice of primary vendor and its exclusion of extraction do not.
**Related:** ADR 0009 (Vertex embeddings — unchanged), ADR 0011 (ACL-filtered retrieval),
ADR 0016 (KT console), ADR 0022 (additive GraphRAG)

## Context

ADR 0023 put a fallback chain behind the answer transport and kept one vendor primary.
Extraction was deliberately left out of it: "a background job with durable bounded
retries; a provider outage delays it rather than failing anybody's request."

Production disagreed. Over the thirty days before this ADR, **every** call to that vendor
returned HTTP 400. A sample of 200 log lines across 2026-09-08 → 2026-09-12 was 100% 400.
Two surfaces were affected at once and for the same reason:

* the KT copilot (`POST /v1/kt/{code}/ask`) answered 503 — seven times in one day for one
  real recipient;
* nightly extraction recorded `failure=provider_permanent` on every document it touched.

A request trace shows the shape exactly: Vertex embedding 200 → `vector_search returned=30
k=30 elapsed_ms=65` → the model call 400 → 503. **Retrieval was never the problem.** The
ACL held, the evidence was found, and the only thing that failed was the sentence at the
end.

Neither `JUTSU_ANSWER_MODEL` nor `JUTSU_EXTRACTION_MODEL` was set in production, so both
paths defaulted to the same model id. A revoked or wrong key would have been 401, so the
model id was the common factor — but the conclusion that matters is not which id it was.
It is that **one vendor's decision made JUTSU's two model-dependent surfaces unavailable
for five days, and nothing in the system could route around it**, because the fallbacks
ADR 0023 introduced were never given keys and extraction had no chain at all.

The second lesson is about the ADR 0023 reasoning itself. "A background job that retries
is not urgent" is true of a transient outage and false of a permanent one: bounded retries
against a permanent refusal are five identical rejections and then a dead letter. The
corpus stopped gaining claims, the graph stopped gaining nodes, and no request failed
anywhere — which is precisely why it went unnoticed for five days.

## Decision

**One chain, three vendors, both callers.**

**1. The vendor is removed, not demoted.** No `anthropic` import, no `anthropic`
dependency in any manifest or in `uv.lock`, no `ANTHROPIC_API_KEY` in `.env.example` or in
the deploy workflow, and no reference in any production source file.
`packages/llm/tests/test_no_legacy_provider.py` walks the repository as files and fails if
one comes back. `docs/adr/` is exempt: an ADR that may not name what it superseded is a
record of nothing.

**2. The order is Cerebras → OpenRouter → Groq**, from `LLM_PROVIDER_ORDER`. Three
independent companies serving one model family. The order is capability-neutral because
they run the same model, so it is really a cost-and-latency order, and it is configuration
rather than a constant anybody must redeploy to change.

**3. One model family across all three, deliberately.** `gpt-oss-120b` on Cerebras,
`openai/gpt-oss-120b` on OpenRouter and Groq — each independently overridable. The citation
gate decides whether an answer is shown at all: the model must emit `[n]` markers against
numbered passages or emit `INSUFFICIENT_EVIDENCE` and nothing else. That is a formatting
contract, and models from different families keep it differently. A fallback from another
family does not degrade gracefully — its answers get discarded by the gate at exactly the
moment the primary is down. One family across three independent infrastructures keeps the
formatting constant while keeping the failure domains separate. The cost, stated rather
than hidden: a flaw in the model family itself would affect all three at once, which is
what the per-provider model overrides exist for.

Defaults were read from each vendor's own catalogue on 2026-09-12 and are pinned by
`TestTheVerifiedDefaults`. OpenRouter's entry was verified live (`GET /api/v1/models`):
131,072 context, 117,964 max completion tokens, `response_format` supported, $0.04/$0.17
per million tokens — the cheapest tier, so a fallback cannot become a cost incident the
way a frontier default at $5/$30 would.

**4. Extraction joins the chain.** `jutsu_worker.extraction` now calls the same
`FailoverTransport` the API answers through, and the transport protocol moved from
`complete(system, prompt) -> str` to `generate(LLMRequest) -> LLMResponse`. That is not
cosmetic: **provenance must name the model that actually answered.** Non-negotiable 1
requires `model` on every piece of evidence, and with a chain the configured model and the
answering model are different things whenever a fallback fires. `extraction_runs.model` and
every claim's `payload_json.model` are now written from the response, with the vendor
recorded alongside as `provider`. A run that never reached a provider records
`unresolved` rather than a plausible model id for a call that never happened.

**5. The package moved out of the app.** `jutsu_api.llm` became the `jutsu-llm` workspace
package, because the worker needs the identical chain and an app may not import another
app. Two provider frameworks would be two failure taxonomies, two ordering rules and two
sets of credentials to keep in step. A test asserts `jutsu_llm` imports neither app.

**6. Embeddings are untouched.** Vertex `gemini-embedding-001` at 768 dimensions stays
exactly as ADR 0009 specifies. This ADR is about generation. Query and document vectors,
the HNSW index, the `task_type` split and the normalisation are all unchanged, and
changing the embedding provider would invalidate every stored vector in the corpus.

**7. The chain's exhaustion is typed.** `AllProvidersFailed` subclasses the
`ServiceUnavailable` the API already renders as a 503 — same status, same code, same
sentence, empty `details`, so no caller can learn from an error that a chain exists or
which vendor was unwell. It carries `error_class` as an attribute for the **worker**,
which is not an HTTP caller: `jutsu_worker.ingest.classify` maps `rate_limited`, `timeout`
and `unavailable` to `provider_transient` (retried with backoff) and `refused` and
`not_configured` to `provider_permanent` (not retried). Before the chain that distinction
came from a vendor SDK's exception hierarchy; it is the same decision from a different
source.

Note what reaching that classification now means: **every** configured vendor failed the
same request. A single vendor failing falls over to the next one and the job succeeds, so
it never reaches `classify` at all.

## Consequences

**A vendor answering 400 to everything now costs one wasted attempt per request**, not the
whole service. This is the failure that happened, tested directly by
`test_a_vendor_refusing_everything_no_longer_stops_the_system`.

**A request genuinely malformed by JUTSU is refused three times instead of once.** That is
the deliberate cost of `ProviderRefused` continuing the chain rather than aborting it, and
it is bounded, fast (no retries, no backoff) and loud — every attempt logs
`error_class=refused` with the provider's name, and `GET /v1/ops/answer-providers` shows a
provider that refuses everything. Hiding a programming error would require the logging to
be absent, not the fallback.

**The deploy can now produce a service with no model provider at all.** The vendor secret
used to be mounted unconditionally; the three replacements are resolved by `describe` and
skipped when absent, the same shape as the graph secrets, because an availability layer
must never be the reason a release cannot ship. A deploy that resolves zero of them emits
a workflow **warning** naming the three secrets: `/v1/ask` answers 503 and extraction jobs
are never enqueued. That is a real operational precondition — at least one of
`jutsu-cerebras-api-key`, `jutsu-openrouter-api-key`, `jutsu-groq-api-key` must exist in
Secret Manager before this ships — and it is stated here rather than discovered later.

**Answers may now be produced by a different vendor between two questions**, and nothing
in the product surfaces that. Which provider answered is observability
(`llm_request_success` with `provider`, `model`, `fallback_used`), never part of a
response — §11, and the same reason the error sentences are identical across providers.
For extraction it *is* persisted, because there it is provenance rather than telemetry.

**Three vendors is three sets of credentials to rotate** and three catalogues to watch for
retirements. `docs/deploy.md` §13 carries the checklist; the defaults are pinned by tests
so a vendor's change becomes a visible diff rather than a silent behaviour change.

**Extraction's corpus may now contain claims from more than one vendor.** Re-extraction
supersedes rather than overwrites, so this is visible rather than confusing: each run's row
and each claim's payload names the model and the vendor that produced it. A corpus
extracted by one model and re-extracted by another is a question that can be *asked* of
the database, which it could not be before.

## Alternatives considered

**Keep the vendor as a fourth fallback.** Rejected: the removal was the instruction, and
the operational argument agrees with it. A provider that returned 400 to 200 consecutive
sampled requests over thirty days is not a fallback, it is a guaranteed wasted attempt on
every request that reaches it.

**Different model families across providers for diversity.** Rejected — see decision 3.
Diversity of *infrastructure* is what an availability layer needs; diversity of *output
format* is what the citation gate punishes.

**Leave extraction on a single vendor.** Rejected: that is ADR 0023's position and it is
the one production disproved. It cost five days of a corpus not growing, with no failing
request anywhere to notice.

**Use one provider's own multi-model routing instead of our chain.** Rejected as the only
layer: OpenRouter's `models` array is used *inside* our single OpenRouter attempt, which is
a second layer of resilience and not a replacement for the first. A chain that lived
entirely inside one vendor would still be one vendor's availability.

**Swap the embedding provider at the same time.** Rejected explicitly. Every vector in
`chunks.embedding` was produced by `gemini-embedding-001` at 768 dimensions against an
HNSW index built for that width; changing the model means re-embedding the corpus, and
this ADR is about a failure that had nothing to do with embeddings.
