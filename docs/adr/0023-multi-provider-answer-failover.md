# ADR 0023 — Multi-provider answer failover

**Status:** accepted
**Date:** 2026-09-12
**Related:** ADR 0011 (ACL-filtered retrieval), ADR 0016 (KT console), ADR 0022 (additive
GraphRAG)

## Context

Every grounded answer JUTSU produces — `/v1/ask`, the KT copilot, the KT handover summary —
goes through one method: `AnswerTransport.complete(system, prompt)`. Behind it was
`AnthropicTransport`, one class, one vendor, one `anthropic` call. When Anthropic is rate
limiting, having a bad afternoon, or unreachable from `asia-south1`, every one of those
surfaces returns 503 and the person asking sees "the answer service did not respond".

Retrieval is unaffected by that outage — pgvector answers, the ACL holds, evidence is
returned — so the failure is narrow and total at the same time: everything works except
the sentence at the end.

## Decision

A sequential provider chain, entirely inside the existing transport seam.

**1. The seam does not move.** `jutsu_api.llm.FailoverTransport` implements the same
`AnswerTransport` protocol, so `get_answer_transport()` returns it and nothing else
changes. Retrieval, ACL filtering, prompt composition, evidence numbering, the citation
gate, the single retry and the refusal are all exactly where they were — above and below
the chain, untouched. A fallback provider is invisible to every one of them.

**2. Claude stays primary.** Order is `claude, cerebras, openrouter, groq`, configurable
through `LLM_PROVIDER_ORDER`. In the ordinary case Claude answers and no other vendor is
contacted or paid.

**3. Sequential, never speculative.** One request is at most one attempt per provider,
bounded by `LLM_MAX_PROVIDER_ATTEMPTS` (4). Asking four vendors in parallel would answer
marginally faster and cost four times as much on every question, including the ones the
primary answers immediately.

**4. Every provider receives the identical request.** `LLMRequest` is frozen and built
once, before the loop. No attempt sees a trimmed prompt, an appended error, or
re-retrieved evidence — the same two strings reach whichever vendor answers.

**5. The failure taxonomy decides what falls over.** `timeout`, `rate_limited` and
`unavailable` (5xx, connection reset, malformed 200, empty completion) move to the next
provider. `refused` — a 4xx that is not 429 — never retries *that* provider and still
moves on, which is discussed below. Nothing is retried against the same vendor: a retry
budget on top of a provider budget is how a request outlives its own deadline.

**6. The budget is bounded twice.** Each provider gets the smaller of
`LLM_PROVIDER_TIMEOUT_SECONDS` and whatever remains of `LLM_TOTAL_TIMEOUT_SECONDS`, so a
first provider that hangs cannot consume the whole request and leave the rest nothing.

**7. Model ids are configuration.** Defaults were read from each vendor's own documentation
on 2026-09-12 and are recorded below; `docs/deploy.md` §13 says to re-check them. A model
that has been retired answers 4xx, which is `refused`, which skips that provider.

## Model selection

| Provider | Model | Why |
|---|---|---|
| Claude | `JUTSU_ANSWER_MODEL`, default `claude-opus-5` | **Unchanged.** The existing production value; this layer does not get to re-pick the primary. |
| Cerebras | `gpt-oss-120b` | Listed in their catalogue as production, 131k context on paid tiers, the stronger of the two entries for reasoning and instruction following. |
| Groq | `openai/gpt-oss-120b` | Their model page marks it **production** rather than preview, 131k context. |
| OpenRouter | *no default* | Its catalogue is a marketplace of slugs that retire continuously. A stale default would look configured and fail every call, so unset means "not configured" and the provider is skipped. |

The two middle fallbacks are the same model family on two independent providers, and that
is deliberate: the citation gate is a demanding instruction-following test — emit `[n]`
markers, or emit `INSUFFICIENT_EVIDENCE` and nothing else — and a fallback that formats
differently produces refusals rather than answers. One family behind two vendors means the
gate sees what it was tuned for, while the outage risk stays independent.

**Context length matters more here than it looks.** The prompt carries up to `k` retrieved
passages; 131k is comfortable for the k=30 default and leaves the fallbacks no worse off
than the primary.

## Consequences

**A deployment with only `ANTHROPIC_API_KEY` behaves exactly as it did before.** One
provider, one attempt, the same error sentences. Everything else is opt-in by adding a key.

**A deployment with no Claude key still answers.** `answers_configured()` became a
chain-wide question rather than an Anthropic-key question — gating on the primary's key
would have refused every request while a configured Groq sat idle.

**`refused` continues the chain, and that is a considered reading of "do not hide
programming errors".** Retrying the same provider cannot help and is never done. But
stopping the chain would let one vendor's stricter validation, or one key nobody rotated,
take down a request three other providers would have answered. The cost: a request that is
genuinely malformed by JUTSU is refused four times instead of once — bounded, fast (no
retries, no backoff), and loud, because every attempt logs `error_class=refused` with the
provider's name and the admin diagnostic shows a provider that refuses everything. It is
observable rather than hidden, which is what the rule is actually protecting.

**An ungrounded answer is not a provider failure.** The citation gate runs downstream and
is unchanged: a fallback that answers without citations gets the existing one retry and
then the existing refusal. Treating "the model did not cite" as a provider fault would let
the chain shop for a vendor willing to produce an uncited answer, which is the opposite of
non-negotiable 3. An *empty* completion is different and is a provider fault, because
handing "" to the gate would render a provider malfunction as "the evidence does not
support this".

**Extraction is deliberately not in the chain.** `apps/worker` still calls Anthropic
directly. It is a background job with durable bounded retries: a provider outage delays
extraction rather than failing a user's request, and giving it a second vendor would change
what the corpus was extracted with — versioned in `extraction_runs` — for an availability
gain the queue already provides.

**There is no streaming to protect.** JUTSU's answer path is request/response; the whole
fallback decision happens before any byte reaches the caller. If streaming is added later,
the rule has to be written then: fall over before the first token, never mid-answer.

**No metrics system exists**, so observability is the structured logs the rest of the
platform uses: `llm_provider_attempt`, `llm_provider_fallback`, `llm_request_success`,
`llm_request_failed`, `llm_budget_exhausted`. They carry provider, model, error class,
latency and counts — never a prompt, never an answer, never a key. The prompt is the one
string in this layer that contains a customer's retrieved evidence, which makes §4.9 matter
here more than anywhere.

**A fresh HTTP client per call.** A module-level `AsyncClient` is a connection pool bound
to the event loop that created it, and this repository has paid for that twice — the
database engine, and the Neo4j driver in ADR 0022, where a pool outliving its loop failed a
test thirty minutes into preflight. A request about to spend seconds on a model can afford
a handshake.

## What was rejected

**Parallel generation with the first answer winning.** Better latency, four times the cost
on every question, and four times the tokens sent to four vendors for a question one of
them would have answered. §27 rules it out and it would also quadruple the surface over
which a prompt containing customer evidence travels.

**Retries within a provider.** A 429 from a rate-limited vendor is not better on the second
ask a second later, and a retry budget nested inside a provider budget inside a total
budget is how a request outlives its deadline while every individual bound looks correct.

**Letting OpenRouter be the whole fallback story.** It has its own model routing and we use
it — `OPENROUTER_FALLBACK_MODELS` becomes its `models` array, tried inside our single
attempt — but depending on one intermediary for all resilience replaces four failure
domains with one.

**Renaming `ANTHROPIC_API_KEY` to `JUTSU_CLAUDE_API_KEY`** for symmetry with the new keys.
It is an existing production secret mounted by an existing pipeline; renaming it to make a
table look tidy is an outage in exchange for nothing.

**A live provider probe in `/readyz`.** Readiness is polled continuously; probing four
vendors would spend money on a health check and make the endpoint slow exactly when a
provider was down. The diagnostic (`GET /v1/ops/answer-providers`, behind `org:read`)
reports configuration, and the logs report what actually happened.
