// Reverses 002. Drops only what 002 created, and drops no data.
//
// IF EXISTS throughout, so a partially-applied 002 can still be rolled back — the state a
// migration is most likely to be in when somebody needs to reverse it.
//
// The order is the reverse of creation, so a reviewer can check this file against its up
// file by eye. Neo4j has no dependencies between these objects, so the order is for the
// reader rather than for the server.
//
// Note what this does NOT do: it does not remove the nodes or relationships ingestion
// wrote. Dropping an index is a schema change; deleting the graph is data loss, and a
// downgrade that silently discarded a corpus's worth of extracted relationships would be
// unrecoverable — re-running extraction against every document is a paid operation.

DROP INDEX evidenced_by_org_document IF EXISTS;

DROP INDEX mentions_org_document IF EXISTS;

DROP INDEX evidenced_by_id IF EXISTS;

DROP INDEX mentions_id IF EXISTS;

DROP INDEX meeting_org_name IF EXISTS;

DROP INDEX decision_org_name IF EXISTS;

DROP INDEX project_org_name IF EXISTS;

DROP INDEX person_org_name IF EXISTS;

DROP INDEX document_org_document_id IF EXISTS;

DROP CONSTRAINT meeting_org_key IF EXISTS;

DROP CONSTRAINT decision_org_key IF EXISTS;

DROP CONSTRAINT person_org_key IF EXISTS;
