# ADR 0007 — Neo4j tenancy is enforced in the session, because the database cannot

- **Status:** accepted
- **Date:** 2026-08-27
- **Slice:** S2

## Context

ADR 0003 exists because a superuser silently bypassed row-level security: every isolation
test passed against policies that never engaged. The lesson recorded there is that tenant
isolation must be enforced by something that cannot be forgotten, and verified against
something that can actually fail.

Neo4j offers nothing to enforce it with.

| Mechanism | Postgres | Neo4j 5 Community | Neo4j AuraDB |
|---|---|---|---|
| Row/node-level policy | `CREATE POLICY`, `FORCE ROW LEVEL SECURITY` | **none** | none |
| Database per tenant | schema/database + `GRANT` | one database only | tier-dependent |
| Property existence constraint | `NOT NULL` | **Enterprise only** | tier-dependent |
| Fails closed on a forgotten filter | yes — predicate is NULL, no rows | **no — returns every tenant** | no |

The last row is the whole problem. In Postgres, a query that forgets its tenant filter
returns *nothing*. In Neo4j the same mistake returns *everything*, with no error, in a
result set that looks entirely normal.

## Decision

`org_id` is enforced in `GraphSession`, and there is no other way to reach the graph.

1. **`GraphSession.run` refuses any Cypher that does not reference `$org_id`.** Forgetting
   to scope raises `UnscopedQuery` rather than widening a result set.
2. **The caller cannot supply `org_id`.** It is bound from the `UUID` the session was
   opened with. Passing it as a parameter raises — not silently overwritten, because
   correcting an attempt to change tenant into a successful query against the right one
   records nothing and teaches nobody.
3. **`read_session` is the default; `write_session` is explicit.** Two mechanisms, because
   neither suffices alone (below).
4. **Values are always parameters.** The only text ever interpolated is a label or
   relationship type from `labels.py`, which is a closed enum with an identifier check
   applied at import.
5. **Every transaction carries a server-side timeout** — 30s on the request path, 120s for
   DDL. Enforced by Neo4j, so a hung client cannot hold a transaction open.
6. **`ddl_session` is the single unscoped path**, used only by the migration runner and
   named to be conspicuous in review, exactly as `unscoped_session` is in `packages/db`.

### Why read-only needs two mechanisms

`default_access_mode=READ_ACCESS` is the driver-level control, and on a cluster or AuraDB
it routes to a follower which rejects writes outright. **On the single-instance Community
container used in development it is only a routing hint and blocks nothing.** Believing
otherwise is how a control becomes decorative — the ADR 0003 failure again, in a new
store. So a static check on the Cypher text rejects the write clauses as well, and that
one works everywhere. `test_write_clauses_are_refused_in_a_read_session` exercises six.

## Consequences, including the uncomfortable ones

- **The `$org_id` check is textual.** It proves the parameter is *referenced*, not that it
  is referenced correctly: `MATCH (n) WHERE n.org_id = $org_id OR true` passes. The threat
  model is a developer forgetting, not a developer attacking their own product, and for
  forgetting it is exact. A stronger guarantee needs a query builder rather than raw
  Cypher, which is a later slice if the templates of §12 justify one.
- **Write-clause detection does not classify procedure calls.** `CALL` into an APOC write
  procedure is not caught. The LLM-generated Cypher path of §22 will need a procedure
  allowlist on top of this; it is not built here because there is no generator yet, and a
  filter written against no caller is a filter nobody has tested.
- **No database-level guarantee that `org_id` exists on a node.** Community cannot express
  a property-existence constraint. A write that omits `org_id` succeeds and creates a node
  no scoped query can ever see — invisible rather than leaked, which is the right failure
  direction, but still a real gap. Migration 001 says so in a comment where somebody
  looking for the constraint would expect to find it.
- **Fulltext indexes cannot be compound with `org_id`.** `topic_name` and `decision_text`
  narrow; they do not authorise. Any query using them must still filter, and the session
  check enforces that it does.
- Cross-tenant isolation is proven by `test_graph_tenancy.py`, against a store that
  genuinely contains a second tenant's nodes — organisation ids are random per test, so
  no assertion there is vacuous the way an empty-database test would be.

## The migration runner

Numbered Cypher, paired up/down files, a ledger node, and a stored checksum re-verified on
every run. Three decisions worth recording:

- **A migration with no down file is refused at load**, not at rollback. §4.12 requires
  reversibility, and the moment you discover it is missing is the moment you least want to.
- **`upgrade` verifies checksums; `downgrade` does not.** Editing an applied migration
  raises, which is the point — but that also means a poisoned ledger blocks `upgrade`, and
  recovery has to be possible. `downgrade` deliberately does not check, so the escape
  hatch is `make migrate-graph-down` followed by `make migrate-graph`.
- **DDL runs one statement per transaction.** Neo4j refuses to mix schema and data
  operations in one transaction, and fails at commit with a message that does not name the
  cause.

## Rejected

- **A tenant per Neo4j database.** Community supports one. On Aura it depends on the tier,
  and a tenancy model that changes shape with a billing decision is not a tenancy model.
- **Trusting `READ_ACCESS` alone.** See above; it is inert on the development topology,
  which is where most queries are written.
- **`notifications_disabled_classifications` to quiet expected server notices.** It is a
  preview driver API that emits its own warning on every session. `_ensure_ledger` checks
  for the constraint before creating it instead, which removes the notice without taking
  on an unstable dependency.
