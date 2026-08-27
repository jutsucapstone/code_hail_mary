// Migration 001 — constraints and indexes (spec §7).
//
// Every statement is IF NOT EXISTS, so applying this to a database that already has it
// is a no-op. The ledger in migrations.py already prevents a re-run; this makes the file
// safe even if it is applied by hand.
//
// Plain Cypher DDL only. APOC is available in the local container and on AuraDB, but a
// migration is the one thing that must run identically on both, and the set of APOC
// procedures Aura permits is not the set the community image ships.
//
// Note what is NOT here: a constraint requiring org_id to exist. Property existence
// constraints are Neo4j Enterprise; Community cannot express them, and prod is AuraDB
// where the available tier decides. So org_id is enforced entirely by the application —
// GraphSession.run refuses any query that does not reference $org_id. ADR 0007 records
// that this is the whole mechanism, with nothing underneath it.

// Uniqueness — the three identities that must not duplicate within an organisation.
// Each is compound on org_id, so two tenants may each have a person with the same
// address without colliding.
CREATE CONSTRAINT person_org_email IF NOT EXISTS
  FOR (p:Person) REQUIRE (p.org_id, p.email) IS UNIQUE;

CREATE CONSTRAINT doc_source_id IF NOT EXISTS
  FOR (d:Document) REQUIRE (d.org_id, d.source_system, d.external_id) IS UNIQUE;

CREATE CONSTRAINT project_key IF NOT EXISTS
  FOR (pr:Project) REQUIRE (pr.org_id, pr.key) IS UNIQUE;

// The decision ledger is queried as "what was decided, for this org, in this window",
// so the index leads with org_id and the range column follows it.
CREATE INDEX decision_time IF NOT EXISTS
  FOR (d:Decision) ON (d.org_id, d.decided_at);

// Fulltext, for the lexical half of the hybrid retrieval in §12. Neo4j fulltext indexes
// cannot be compound with org_id, so a query using either of these MUST still filter on
// org_id afterwards — the index narrows, it does not authorise.
CREATE FULLTEXT INDEX topic_name IF NOT EXISTS
  FOR (t:Topic) ON EACH [t.name];

CREATE FULLTEXT INDEX decision_text IF NOT EXISTS
  FOR (d:Decision) ON EACH [d.statement];
