# ADR 0013 — A gate reports what it measured, and nothing else

- **Status:** accepted
- **Date:** 2026-08-28
- **Slice:** S9

## The sentence this ADR exists to make true

> **"Not measured" is an outcome, not a failure and not a pass.** A clause the harness
> could not evaluate says so, carries no number, and blocks a milestone under `--strict`.

## Context

§21 ends each phase at a gate, and CLAUDE.md rule 8 says every number this project quotes
comes from `make eval`. Until this slice, `make eval` and `make gate` both `exit 1` with
"implemented in S9", so no figure the project could state had a mechanism behind it. That
is tolerable for Phase 1, where the clauses are mostly properties a test suite already
asserts. It stops being tolerable at S14 and S23, which ask for an extraction precision
and a groundedness percentage — numbers with no natural home outside a harness.

The design question was not "how do we run eleven checks". It was **what a check is
allowed to say when it cannot run**, and that turned out to decide everything else.

## The failure being designed against

A boolean gate has one way to express "I could not look", and it is `False`.

Consider `documents_ingested` on a laptop with the containers stopped. `count(*)` over a
database that was never named returns nothing; `0 >= 45_000` is false; the gate reports a
failure. Every word of that is well-formed and the conclusion is worthless — the corpus
was never claimed to be there. Run it a few times and the reader learns the gate is noisy,
which is the point at which a real failure stops being visible.

The same shape appears three more times in M1:

- A test suite that **skipped** exits 0. `all 7 ACL tests pass` is satisfied by a suite
  that ran nothing, which is precisely the defect the root `conftest.py` was written to
  fix — 112 tests silently skipping while `make preflight` reported green.
- `migrations reversible` cannot be run against a seeded database at all: the round trip
  would destroy the corpus every other clause measures.
- `seed-run token cost recorded` has no answer if nobody wrote the cost down. Reporting 0
  would be inventing a measurement of a run that may have spent thousands of tokens.

In each case the honest answer and the convenient answer differ, and the convenient one is
green.

## Decision

**Three outcomes: `passed`, `failed`, `not_measured`.**

`CheckResult` enforces the distinction rather than documenting it — constructing a
`not_measured` result with an `observed` or a `threshold` raises. Rule 8 becomes an
invariant a future check cannot talk its way past.

`--strict` promotes `not_measured` to failure. It is not the default because the default
reader is a developer with the containers stopped, and telling them their code failed
would be a lie. It is what CI and a milestone sign-off use, because a gate that certifies
eleven clauses having measured six is not a gate.

Five decisions follow from that one.

1. **A skip is `not_measured`, never `failed` and never `passed`.** Blaming the code for
   the harness's circumstances is as wrong as excusing it. The result names how many tests
   did not run.
2. **An unexpected exception is `not_measured`, and only the exception *type* is
   recorded.** An exception message can carry a row, a query or an address; §4.9 applies to
   a gate report exactly as it applies to a log line.
3. **Every database measurement runs through `org_session`**, as the restricted
   `jutsu_app` role under RLS. A gate reading through `MIGRATION_DATABASE_URL` would
   describe a database the application cannot see — the mistake ADR 0003 exists for, and
   one that leaves every isolation test still passing.
4. **Reversibility is measured against the test database.** `JUTSU_TEST_MIGRATION_URL`,
   never the seeded one. The related trap stays reported rather than worked around:
   `migrate-pg-down` refuses once a document has been superseded, and on such a database
   the clause is legitimately unmeasured.
5. **The one check that writes asks first.** `seed_idempotent` cannot observe "a second
   `make seed` adds zero rows" without performing a second `make seed`. It requires
   `--allow-writes` and is otherwise unmeasured, and it takes the ingestion entry point as
   an injected callable — `jutsu_evals` is a package and must not depend on `apps/worker`.

## What this costs

The gate is slower and less satisfying than a boolean one. On a machine with no corpus it
reports five unmeasured clauses instead of a confident red or green, and somebody has to
read the reasons. That is the intended trade: the alternative is a number that looks like
a measurement and is not one, which is worse the more it is trusted.

It also means **S9 landing is not an M1 pass**, and the harness says so in as many words —
the summary line refuses to print "gate PASSED" over any run with an unmeasured clause,
whatever the exit code is.

## Alternatives rejected

**Two outcomes, with unmeasured clauses omitted from the report.** Then the tally is
`6 passed, 0 failed` and the reader has to notice the missing five. Absence is the least
visible signal available.

**Two outcomes, treating unmeasured as failure.** Correct for CI, wrong for a laptop, and
it makes the ordinary local state permanently red — which trains people to ignore red.

**Deriving thresholds from configuration.** `EMBEDDING_DIM` is configuration and an
operator can change it; §21 says the gate must see 768. If the two disagree the gate has
to report a failure, which it can only do by holding the spec's number itself.

## Consequences

- `evals/reports/phase-N-<utc>.json` is committed, so §18's "name the commit" is possible.
  Run receipts under `evals/runs/` are not: they are local evidence about a local corpus,
  and they store a hash of the corpus path rather than the path itself.
- S14 and S23 add entries to the same registry and inherit all of the above.
- A future phase gate that wants a fourth outcome should read this ADR first.
