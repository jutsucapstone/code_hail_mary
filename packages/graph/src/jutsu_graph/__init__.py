"""JUTSU graph — Neo4j driver, tenancy, schema migrations and bitemporal helpers (§7).

Three things live here and nothing else does yet:

  * `driver` — the connection, and the org-scoped session that makes tenancy impossible
    to forget. Neo4j has no row-level security, so `$org_id` is the whole mechanism.
  * `migrations` — numbered Cypher, a ledger, checksums, and a real `downgrade` (§4.12).
  * `temporal` — `supersede` and `as_of`, written before there is anything to store,
    because retrofitting bitemporality later means rewriting every edge (§7).

Cypher templates, entity resolution and the GraphRAG traversal of §12 are later slices.
Nothing here reads or writes application data; it is the layer those will be built on.
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
    "ChecksumMismatch",
    "DdlSession",
    "GraphSession",
    "GraphSettings",
    "Migration",
    "MissingGraphSettings",
    "NaiveTimestamp",
    "NodeLabel",
    "RelationshipType",
    "UnknownLabel",
    "UnscopedQuery",
    "UntemporalQuery",
    "WriteInReadSession",
    "applied_versions",
    "as_of",
    "close_driver",
    "current_filter",
    "ddl_session",
    "downgrade",
    "get_driver",
    "get_graph_settings",
    "identifier",
    "load_migrations",
    "node_label",
    "ping",
    "read_session",
    "relationship_type",
    "supersede",
    "temporal_filter",
    "temporal_properties",
    "upgrade",
    "write_session",
]
