"""JUTSU graph — Neo4j driver, tenancy, schema migrations, bitemporality and GraphRAG (§7, §12).

  * `driver` — the connection, and the org-scoped session that makes tenancy impossible
    to forget. Neo4j has no row-level security, so `$org_id` is the whole mechanism.
  * `migrations` — numbered Cypher, a ledger, checksums, and a real `downgrade` (§4.12).
  * `temporal` — `supersede` and `as_of`, written before there was anything to store,
    because retrofitting bitemporality later means rewriting every edge (§7).
  * `knowledge` — what an extraction claim becomes as nodes and edges, as pure functions.
  * `ingest` — writing those edges idempotently, superseding rather than deleting.
  * `retrieval` — bounded, read-only traversal that returns **chunk identifiers**.
  * `health` — a bounded, cached reachability probe for `/readyz`.

**This layer is additive and it is not an authorization surface (ADR 0022).** Retrieval
here returns identifiers; the caller resolves them through Postgres under `ACL_PREDICATE`,
which is where every decision about who may read what is made. Nothing in JUTSU depends on
this store being up: vector retrieval does not touch it, and the hybrid path falls back to
vector alone when it is unreachable, unconfigured or slow.

Entity resolution (§11) is still a later slice. Two spellings of one name are two entities
here, deliberately — `knowledge.entity_key` normalises case and punctuation and nothing
more, because fuzzy merging belongs in `resolution_queue` with a human above the threshold.
"""

from jutsu_graph.driver import (
    DdlSession,
    GraphSession,
    GraphSettings,
    MissingGraphSettings,
    UnscopedQuery,
    WriteInReadSession,
    close_driver,
    ddl_session,
    get_driver,
    get_graph_settings,
    ping,
    read_session,
    write_session,
)
from jutsu_graph.health import GraphStatus, probe, reset_probe_cache
from jutsu_graph.ingest import GraphSyncReport, sync_document
from jutsu_graph.knowledge import (
    CLAIM_LABELS,
    ClaimRecord,
    DocumentRef,
    EntityRef,
    GraphEdge,
    UnmappedClaim,
    edge_id,
    entity_key,
    normalise_name,
    plan_edges,
)
from jutsu_graph.labels import (
    NodeLabel,
    RelationshipType,
    UnknownLabel,
    identifier,
    node_label,
    relationship_type,
)
from jutsu_graph.migrations import (
    ChecksumMismatch,
    Migration,
    applied_versions,
    downgrade,
    load_migrations,
    upgrade,
)
from jutsu_graph.retrieval import (
    DEFAULT_GRAPH_LIMIT,
    GraphCandidate,
    GraphHop,
    candidate_names,
    expand_from_documents,
    graph_candidates,
    seed_from_names,
)
from jutsu_graph.temporal import (
    NaiveTimestamp,
    UntemporalQuery,
    as_of,
    current_filter,
    supersede,
    temporal_filter,
    temporal_properties,
)

__all__ = [
    "CLAIM_LABELS",
    "DEFAULT_GRAPH_LIMIT",
    "ChecksumMismatch",
    "ClaimRecord",
    "DdlSession",
    "DocumentRef",
    "EntityRef",
    "GraphCandidate",
    "GraphEdge",
    "GraphHop",
    "GraphSession",
    "GraphSettings",
    "GraphStatus",
    "GraphSyncReport",
    "Migration",
    "MissingGraphSettings",
    "NaiveTimestamp",
    "NodeLabel",
    "RelationshipType",
    "UnknownLabel",
    "UnmappedClaim",
    "UnscopedQuery",
    "UntemporalQuery",
    "WriteInReadSession",
    "applied_versions",
    "as_of",
    "candidate_names",
    "close_driver",
    "current_filter",
    "ddl_session",
    "downgrade",
    "edge_id",
    "entity_key",
    "expand_from_documents",
    "get_driver",
    "get_graph_settings",
    "graph_candidates",
    "identifier",
    "load_migrations",
    "node_label",
    "normalise_name",
    "ping",
    "plan_edges",
    "probe",
    "read_session",
    "relationship_type",
    "reset_probe_cache",
    "seed_from_names",
    "supersede",
    "sync_document",
    "temporal_filter",
    "temporal_properties",
    "upgrade",
    "write_session",
]
