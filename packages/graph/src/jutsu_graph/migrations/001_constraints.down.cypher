// Reverses 001. Drops only what 001 created, and drops no data.
//
// IF EXISTS throughout, so a partially-applied 001 can still be rolled back — which is
// the state a migration is most likely to be in when someone needs to reverse it.
//
// The order is the reverse of creation. It does not matter to Neo4j, which has no
// dependencies between these objects, but a down file that reads as the mirror of its up
// file is one a reviewer can check by eye.

DROP INDEX decision_text IF EXISTS;

DROP INDEX topic_name IF EXISTS;

DROP INDEX decision_time IF EXISTS;

DROP CONSTRAINT project_key IF EXISTS;

DROP CONSTRAINT doc_source_id IF EXISTS;

DROP CONSTRAINT person_org_email IF EXISTS;
