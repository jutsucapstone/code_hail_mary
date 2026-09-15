# ADR 0028 — Ask KT reads extracted claims beside passages

**Status:** accepted
**Date:** 2026-09-15
**Related:** ADR 0016 (KT console), ADR 0024 (answer chain), ADR 0025 (a package reads its
subject), ADR 0027 (what a package carries)

## Context

A knowledge-transfer package already holds structured knowledge. Extraction writes quote-gated
claims — projects, meetings, people, responsibilities, decisions — and the console lists them in
its tabs and the handover report. Ask KT did not read them. It searched passages alone, nearest
first, so "what decisions did A make?" was answered only when the passage recording a decision
happened to sit near the question in embedding space. The Decisions tab beside it could list the
decision the answer had just said it could not find.

The 2026-09-15 isolation audit recorded this as the gap between "all supported knowledge types"
and what Ask KT actually searched.

## Decision

### 1. Two arms, one boundary

Ask KT reads two things, and both run inside the same boundary:

- **Passages.** `search_subject_chunks`, unchanged.
- **Claims.** `jutsu_api.kt_search.claims_for_question` composes `KtScope.conditions` over the
  document each claim's evidence chunk belongs to. That is the same `KT_PACKAGE_PREDICATE` the
  passages, tabs and report use.

The claims arm adds three filters of its own:

- the claim categories the package covers;
- each document's latest finished extraction run;
- a bound of `CLAIM_LIMIT` (12).

A claim on a document outside the package — out of its period, not the subject's, kept back by a
curator — does not exist in the statement, and nothing is filtered in Python.

### 2. A claim is evidence with a real source

A claim becomes a numbered evidence item carrying its own chunk id, document id and chunk span.
The citation gate therefore treats it exactly like a passage, and a citation on it opens the
passage it was extracted from through the KT evidence door.

The model is shown the claim's type, name and summary beside its verbatim quote. It cannot cite a
claim without citing that passage. Stored citations record `kind` (`passage` or `claim`), so a
replayed conversation can still say what a marker named, and the console labels a claim citation
as such.

### 3. Which claims, decided in SQL

- **Intent.** Whole words of the question map to claim types: "decisions" and "decided" to
  `decision`, "who" and "worked" to `person`, and so on.
- **Words.** The question's other words become a PostgreSQL English full-text query over each
  claim's name, summary and quote. They are letters and digits only, joined with `|` and bound as
  one parameter, never interpolated.
- **Order.** Claims of an asked-about type first, then text rank, then the most recent, up to the
  bound.
- **No match, no claims.** A question that names no claim type and shares no claim's words reads
  no claims, and passages answer it as before.

### 4. Unchanged

- **No copilot without the documents category.** A package without `documents` still has no
  copilot (ADR 0025 decision 4).
- **Logs stay counts.** `kt_search_completed` gains a `claims` count, and no claim text reaches a
  log line.

## Consequences

- **Structured questions find structured answers.** "What projects was A responsible for?"
  retrieves A's project and responsibility claims whatever the passage ranking.
- **Cost.** One more bounded, indexed statement per question, and up to twelve more numbered items
  in the prompt.
- **The intent words are English and heuristic.** They decide ranking, never authorization: a
  mismatched word surfaces fewer or different claims, never a claim outside the package.
- **Residue.** Claims are not embedded, so a question phrased with none of a claim's words and none
  of the intent words will not find it through this arm. Embedding claims means a second vector
  column, an embedding job per extraction run and a model-version story; it is not built.

## Alternatives considered

**Embed claims and search them by vector.** Better recall on paraphrase. Rejected for now: see
Residue.

**Send every claim in the package.** Rejected: the prompt would grow without bound, and a large
package would crowd the passages out.

**Ask the model which claim types a question needs.** Rejected: a second paid call and more latency
per question, to decide something a word list decides well enough for ranking.
