# ADR 0002 — `org_id` is denormalised onto `chunks` and `document_acl`

- **Status:** accepted
- **Date:** 2026-08-19
- **Slice:** S1

## Context

Spec §8 gives these shapes:

```
chunks       (id, document_id fk, ordinal, text, char_start, char_end, token_count, embedding)
document_acl (document_id fk, principal_type, principal_id, permission)
```

Neither carries `org_id`. The same section then requires:

> Row-level security on `documents`, `chunks`, `document_acl` keyed on `org_id` —
> enabled **and** forced.

and §4.7 requires "`org_id` on every row, every node, every Cypher template. No
exceptions."

Those cannot all be literally true as written. An RLS policy on `chunks` keyed on
`org_id` needs an `org_id` to key on.

## Options

1. **Policy via correlated subquery.**
   ```sql
   USING (EXISTS (SELECT 1 FROM documents d
                  WHERE d.id = chunks.document_id
                    AND d.org_id = current_setting('app.current_org_id')::uuid))
   ```
   Keeps §8's column list verbatim. Rejected: this predicate runs for every candidate
   row on the hot retrieval path (§12 does a top-30 HNSW scan per question, with a
   p95 < 3s budget), and it makes the ACL join in §12's query harder to reason about
   because two different org checks are then in play at two different layers.

2. **Denormalise `org_id` onto both tables.** Accepted.

## Decision

Add `org_id uuid NOT NULL` to `chunks` and `document_acl`, and key their RLS policies on
it directly.

Integrity is enforced structurally rather than by convention: `documents` gets a unique
constraint on `(id, org_id)`, and both child tables use a **composite** foreign key
`(document_id, org_id) REFERENCES documents (id, org_id)`. A chunk therefore cannot
reference a document belonging to a different org — the database rejects it, so the
denormalised column cannot drift from its parent.

## Consequences

- §8's column list for these two tables is extended by one column. §4.7 is now literally
  satisfied rather than approximately.
- The RLS predicate is a simple column comparison on every table, so the same policy
  shape applies to all three and is trivial to audit.
- Ingestion must set `org_id` when inserting chunks and ACL rows. It cannot silently
  forget: the column is `NOT NULL` and the composite FK rejects a mismatched value.
- §12's ACL-filtered query keeps its `document_acl` join exactly as the spec writes it.
  §17's test 7 — that `EXPLAIN` output still contains the `document_acl` join — remains
  the check that catches anyone quietly moving the filter into Python.
