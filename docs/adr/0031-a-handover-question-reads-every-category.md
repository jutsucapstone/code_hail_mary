# ADR 0031 — A handover question reads every category the package covers

**Status:** accepted
**Date:** 2026-09-22
**Related:** ADR 0025 (a package reads its subject), ADR 0027 (what a package carries),
ADR 0028 (Ask KT reads extracted claims), ADR 0030 (Cited Q&A reads the asker's own memory)

## Context

A recipient's first question in a handover is not a question about one fact. It is "what
should I understand first?", "get me up to speed on A's work", "what should I know about
A?" — the question somebody asks on the day they inherit somebody else's job.

ADR 0028 gave Ask KT a claims arm, and bounded it correctly for the question it was written
for: read the claim types the question **names**, then claims sharing its **words**, at most
twelve. A question about the handover as a whole names no claim type and shares no claim's
words, so that rule read **no claims at all** and the answer stood on passages alone — while
the Decisions, Projects, People, Meetings and Responsibilities tabs one click away listed
exactly what was being asked for.

Traced in production on 2026-09-22, four consecutive Ask KT questions on one package:

```
kt_retrieval_context_created  categories=7 subject_principals=3 windowed=True
vector_search authorization=subject returned=6 k=30 attempts=2 ef_search=400 exhausted=True
kt_search_completed           results=6 claims=0 folders=0      (three of the four)
```

Two separate facts sit in those lines, and only one of them is a defect.

**The passage half is honest.** `exhausted=True` after two rungs returning the same count,
with `hnsw.iterative_scan = strict_order`, means six *is* the package's embedded,
non-superseded, in-package chunk set. No larger `k` finds a seventh. Raising it would spend
latency to re-prove that.

**The claims half was not.** `claims=0` did not mean the package held no claims. It meant
the question had not named one.

A third contributor sat above both: the shared answer prompt says "be concise" and "if the
evidence does not contain enough to answer, respond with exactly INSUFFICIENT_EVIDENCE",
which a model reads as a question about the whole question rather than about each part of
it. A six-part question supported on four parts was refused in full. Every one of the four
production asks took two model calls, which is the citation gate rejecting the first answer
each time.

## Decision

### 1. A question about the handover is a question about every category

`jutsu_api.kt_search.comprehensive(question)` recognises a small, listed vocabulary —
`everything`, `overview`, `handover`, `onboarding`, and phrases like `should know`,
`should understand`, `understand first`, `up to speed`, `walk through`, `full picture`,
matched against the question's words with pronouns removed so "what should **I** know" is
still the phrase "should know".

Such a question is read as naming **every claim category the package covers**. It is not a
classifier and does not need to be: what it decides is how many categories are *ranked*, so
a wrong answer costs a wider or narrower reading of the same package, never a different one.
It errs wide, because the cost of erring wide is a longer prompt and the cost of erring
narrow is the refusal this ADR exists to remove.

### 2. A per-category quota, in SQL

Twelve claims divided five ways is two of each, and a single global ordering does worse than
that: measured against a package holding twenty project claims and twenty decision claims,
one ordering returns twelve claims about one or two categories and none about the others.

So a question naming more than one category runs a second statement over **the same
`WHERE`**: `ROW_NUMBER() OVER (PARTITION BY cl.claim_type …)` ranks each category on its
own, the outer query keeps `:per_type` of each, and orders by that rank across categories —
every category's best claim, then every category's second. Categories the question actually
*named* still lead, so "tell me everything about A's decisions" is still about decisions.

`COMPREHENSIVE_CLAIM_LIMIT` is 25 and `per_type` is the ceiling of `limit / categories`.
`CLAIM_LIMIT` stays 12 for every question that names one category or none, and such a
question runs the statement it always ran.

**The balancing is an ordering.** It runs inside one statement, over the same
`KtScope.conditions`, the same package categories and the same latest-run join, and an
ordering cannot admit a row a filter excluded. Nothing about *which* claims are legible
changed — only how many of each are read.

### 3. A gap is named, not turned into a refusal

`synthesise_answer` takes `comprehensive: bool = False`. When set it appends two rules to
the system prompt, after the six that are already there:

7. cover every part the evidence supports, under a short heading per part;
8. name the unsupported parts in one line each, and reply `INSUFFICIENT_EVIDENCE` only when
   **not one** part of the question is supported.

Ask KT passes it. `/v1/ask` does not, and a test reads the composed system prompt rather
than trusting that sentence, so Cited Q&A sends the two strings it always has.

"No evidence in this package establishes this" is a statement about the retrieved set,
which is the one thing the model can see all of. Rule 1 is unchanged, `_grounded` is
unchanged: every statement about the world still carries a marker, an answer with no marker
at all is still discarded, and a marker naming a passage that was never retrieved is still
discarded.

## Consequences

**What a recipient gets.** A handover question is answered across projects, responsibilities,
decisions, people and meetings, cited, with the categories that hold nothing named as gaps
rather than the whole question refused.

**What it costs.** One question reads at most 25 claims instead of 12. Retrieval is still one
embedding and one search; synthesis is still one model call with the existing single retry.
The quota statement adds a window function over the claims the package predicate already
bounds — the same set the Insights tabs scan per tab.

**What did not change.** `search_subject_chunks`, `k`, the escalation ladder,
`SUBJECT_PREDICATE`, `KT_PACKAGE_RULE`, `KT_PACKAGE_PREDICATE`, exclusions, the period, the
latest-run join, the KT evidence door, `/v1/ask`, `/v1/search`, `/v1/evidence` and every
connector. No new authorization path exists, and no KT reader gained a parameter.

**What is still true and still a limit.** A package holding six passages holds six passages.
Breadth reads more of what is there; it cannot create evidence, and `exhausted=True` on the
vector arm remains the honest report of a thin package. When a handover is thin, the fix is
the package's period or its attached Knowledge Basket files — a curator's decision, not a
retrieval one.

**The one thing to watch.** `comprehensive()` is a word list, so it will occasionally read a
narrow question as a broad one ("do you know about the cutover?"). The consequence is a
longer prompt over the same package. If it ever needs to be tightened, tighten the
vocabulary — never the boundary.
