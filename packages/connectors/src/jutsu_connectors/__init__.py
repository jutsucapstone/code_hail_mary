"""JUTSU connectors — one module per source, all read-only (§6, §9).

Four pieces, and the split between them is the design:

  * `rfc822` — bytes to a `ParsedMessage`. Knows about mail, knows nothing about corpora.
  * `threads` — a corpus to thread components. Knows about reply graphs, not about files.
  * `local` — a directory to documents, with path containment. The reference
    implementation of the `Connector` protocol.
  * `enron` — complete-thread sampling with a reproducible manifest (§19).

**Every connector is read-only.** The `Connector` protocol in `jutsu_core` has no write
method, no connector here adds one, and no OAuth flow in this package will ever request a
write scope (§4.8).

**Nothing here persists anything.** Connectors produce `RawDocument` objects; writing
them to Postgres is the idempotent pipeline of S8, which is also what `make seed` waits
for. Nothing in this package imports `jutsu_db`.

Provider connectors — Gmail, Slack, Jira, GitHub — are Phase 4. They implement the same
three methods against the same models, which is what `local` exists to demonstrate before
there is an OAuth flow in the way.
"""

from jutsu_connectors.enron import (
    DEFAULT_CUSTODIAN_COUNT,
    DEFAULT_SEED,
    DEFAULT_TARGET_MESSAGES,
    CustodianStat,
    SampleManifest,
    SampleResult,
    custodian_of,
    load_documents,
    sample_enron,
)
from jutsu_connectors.local import LocalConnector, PathEscape
from jutsu_connectors.rfc822 import (
    ParsedMessage,
    UnparsableMessage,
    acls_for,
    normalise_message_id,
    parse_message,
    to_raw_document,
    utc_from_timestamp,
)
from jutsu_connectors.threads import MessageLinks, ThreadIndex, build_thread_index

__all__ = [
    "DEFAULT_CUSTODIAN_COUNT",
    "DEFAULT_SEED",
    "DEFAULT_TARGET_MESSAGES",
    "CustodianStat",
    "LocalConnector",
    "MessageLinks",
    "ParsedMessage",
    "PathEscape",
    "SampleManifest",
    "SampleResult",
    "ThreadIndex",
    "UnparsableMessage",
    "acls_for",
    "build_thread_index",
    "custodian_of",
    "load_documents",
    "normalise_message_id",
    "parse_message",
    "sample_enron",
    "to_raw_document",
    "utc_from_timestamp",
]
