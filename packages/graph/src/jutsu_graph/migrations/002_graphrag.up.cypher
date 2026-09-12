// Migration 002 — the additive GraphRAG layer (ADR 0022).
//
// Everything 001 created stays exactly as it is. This migration adds only what the
// extraction-fed knowledge model needs: an identity for each entity kind, the lookups
// retrieval performs, and the relationship indexes that keep an idempotent MERGE from
// scanning.
//
// Every statement is IF NOT EXISTS, so applying it by hand to a database that already has
// it is a no-op — the ledger in migrations.py already prevents a re-run, and this makes
// the file safe regardless.
//
// Note what is NOT here, again: a constraint requiring org_id to exist. Property
// existence constraints are Enterprise; org_id is enforced entirely by the application,
// where GraphSession.run refuses any query that does not reference $org_id (ADR 0007).

// Entity identity. `key` is the derived, hashed identity from knowledge.entity_key —
// compound with org_id, so two tenants may each hold a person of the same name.
//
// Project is deliberately absent: 001 already constrains (org_id, key) on Project as
// `project_key`, and Neo4j refuses a second constraint over the same properties.
CREATE CONSTRAINT person_org_key IF NOT EXISTS
  FOR (p:Person) REQUIRE (p.org_id, p.key) IS UNIQUE;

CREATE CONSTRAINT decision_org_key IF NOT EXISTS
  FOR (d:Decision) REQUIRE (d.org_id, d.key) IS UNIQUE;

CREATE CONSTRAINT meeting_org_key IF NOT EXISTS
  FOR (m:Meeting) REQUIRE (m.org_id, m.key) IS UNIQUE;

// The expansion's entry point: `WHERE d.org_id = $org_id AND d.document_id IN $seeds`.
// A Document node is MERGEd on (org_id, source_system, external_id) — 001's doc_source_id
// — and `document_id` points at the current version, so this is an index rather than a
// constraint: two versions of one document are one node, and the pointer moves.
CREATE INDEX document_org_document_id IF NOT EXISTS
  FOR (d:Document) ON (d.org_id, d.document_id);

// The question-seeded lookup: `WHERE e.org_id = $org_id AND e.name_lower IN $names`.
// One per label, because Neo4j indexes are per-label and a label-less match cannot use
// one — which is why retrieval.py runs one statement per entity label rather than a
// single label-less scan.
CREATE INDEX person_org_name IF NOT EXISTS
  FOR (p:Person) ON (p.org_id, p.name_lower);

CREATE INDEX project_org_name IF NOT EXISTS
  FOR (pr:Project) ON (pr.org_id, pr.name_lower);

CREATE INDEX decision_org_name IF NOT EXISTS
  FOR (d:Decision) ON (d.org_id, d.name_lower);

CREATE INDEX meeting_org_name IF NOT EXISTS
  FOR (m:Meeting) ON (m.org_id, m.name_lower);

// Relationship indexes. The first pair backs `MERGE (…)-[r:TYPE {id: …}]->(…)`, which is
// how ingestion stays idempotent; without them every re-sync scans the relationships
// between the two nodes. The second pair backs the supersession sweep, which matches on
// (org_id, document_id) to close edges a re-extraction no longer asserts.
CREATE INDEX mentions_id IF NOT EXISTS
  FOR ()-[r:MENTIONS]-() ON (r.id);

CREATE INDEX evidenced_by_id IF NOT EXISTS
  FOR ()-[r:EVIDENCED_BY]-() ON (r.id);

CREATE INDEX mentions_org_document IF NOT EXISTS
  FOR ()-[r:MENTIONS]-() ON (r.org_id, r.document_id);

CREATE INDEX evidenced_by_org_document IF NOT EXISTS
  FOR ()-[r:EVIDENCED_BY]-() ON (r.org_id, r.document_id);
