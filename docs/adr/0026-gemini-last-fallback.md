# ADR 0026 — Gemini as the last fallback, and Cerebras out of the default chain

**Status:** accepted
**Date:** 2026-09-14
**Amends:** ADR 0024 — decision 2 (the order), and decision 3 (one model family) for the
last position only.
**Related:** ADR 0024 (the provider chain), ADR 0009 (Vertex embeddings — unchanged)

## Context

Two production facts arrived on 2026-09-13 and 2026-09-14.

**Cerebras could not answer.** It returned HTTP 402 to every call — the account had no
billing — so every question spent its first attempt on a guaranteed refusal. The owner
disabled its key in Secret Manager. Both Cloud Run services still mounted that secret at
`latest`, so the next cold start aborted ("Could not fetch secret … Instance startup will
now abort") and one request received Google's HTML 500 before the mount was removed.

**The owner rotated Groq and asked for Gemini** — with a Gemini API key, "for backup and
other operation": the answer path and extraction both.

ADR 0024 decision 3 rejected mixing model families, because the citation gate is a
formatting contract: `[n]` markers against numbered passages, or `INSUFFICIENT_EVIDENCE`. A
fallback from another family can have its answers discarded at exactly the moment the
primary is down. That argument still stands. This ADR records how a different family is
admitted without contradicting it.

## Decision

**1. The default order is OpenRouter → Groq → Gemini.** The gpt-oss pair answers first; the
different family is asked only once both have failed. Cerebras leaves the default order and
the deploy pipeline's secret list. Its adapter stays, and `LLM_PROVIDER_ORDER` now accepts
any implemented provider rather than only the defaults — so Cerebras returns with one
variable if its billing does. A Cerebras key alone configures nothing.

**2. Gemini goes through Google's OpenAI compatibility layer.**
`generativelanguage.googleapis.com/v1beta/openai/chat/completions`, with a Bearer key read
from `jutsu-gemini-api-key`, as a thin `OpenAICompatibleProvider` subclass. It shares the
error taxonomy and the frozen request and adds no SDK. Google labels the layer beta.

**3. The default model is `gemini-3.6-flash`, chosen by measurement rather than recency.**
The full live contract (decision 4) ran against the stored key on 2026-09-14:

| Model | Result |
|---|---|
| `gemini-3.8-flash` | All four checks failed in both runs: timeouts and 503 "high demand" (176 s, 185 s) |
| `gemini-3.7-flash` | All four checks failed: 503 UNAVAILABLE "high demand" (35 s) |
| `gemini-3.6-flash` | All four checks passed (19 s) |
| `gemini-3.5-flash` | All four checks passed (17 s) |

All four are listed as stable on Google's model page. `gemini-2.5-flash`, also listed as
stable, answered 404 "no longer available to new users" and named `gemini-3.6-flash` as its
replacement. For a last-resort fallback, answering matters more than being newest.
`GEMINI_MODEL` overrides the default.

**4. A provider is admitted by a live contract, and the contract is committed.**
- `apps/api/tests/test_answers_live_contract.py` runs the application's own citation gate
  against each configured provider alone. A question the passages support must come back
  cited, and one they cannot answer must be refused.
- `apps/worker/tests/test_extraction_live_contract.py` runs extraction's prompt and parser,
  requiring JSON with verbatim quotes.

Both are opt-in behind `JUTSU_LIVE_LLM_SMOKE=1`, like the existing smoke test, and CI never
runs them. On 2026-09-14 OpenRouter, the rotated Groq key, `gemini-3.6-flash` and
`gemini-3.5-flash` passed all of it.

**5. Billing on the key's project is a precondition, and the owner confirmed it.** Google's
Gemini API terms let unpaid use improve Google's products and be read by human reviewers,
and say "Do not submit sensitive, confidential, or personal information to the Unpaid
Services". Paid use — a Cloud project with an active billing account — is not used that way.
The deploying account cannot see the key's project, so this is the owner's fact to state.
The owner confirmed on 2026-09-14 that billing is enabled.

## Consequences

**The one-family guarantee now covers the first two links, not all three.** When both
gpt-oss vendors fail, the answer comes from a model whose formatting the gate may treat
differently. The live contract is the evidence it keeps the format today; re-running it is
how to know it still does.

**Gemini's load shows in the logs as `error_class: unavailable` or `timeout`.** As the last
provider, that means the caller gets JUTSU's usual 503 sentence — no worse than a
two-provider chain.

**Extraction provenance may name Gemini.** `extraction_runs.model` and every claim's payload
record the model that answered (ADR 0024 decision 4), so a Gemini-extracted claim is
visible rather than confusing.

**One more data processor.** Evidence reaches Google's Gemini API, under its paid terms,
whenever the first two providers fail. A tenant DPIA must list it.

**Secret mounts stay manual** while `jutsu-deployer` cannot describe secrets. `GEMINI_API_KEY`
is mounted by hand after the deploy. Retiring a provider means removing its mount first and
disabling its secret second — the order that would have avoided this ADR's cold-start 500.

## Alternatives considered

**Gemini on Vertex, as the runtime service account.** There would be no key to leak, and
Vertex serves `asia-south1`. Not chosen, because the owner supplied a Gemini API key. It is
the better fit if data residency becomes a requirement.

**Gemini first.** Rejected: it would undo ADR 0024's reason for one family exactly where it
matters most.

**Pin the newest stable Flash, `gemini-3.8-flash`.** Rejected on measurement: it timed out or
answered 503 on every call of the admission run.

**Delete the Cerebras adapter.** Not needed: out of the default order and the pipeline, it
costs nothing, and it returns with one variable.
