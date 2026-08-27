"""JUTSU retrieval — embeddings today, ACL-filtered search from S7 (§6, §9.3).

What is here: the Vertex transport behind an interface, batching with two bounds, retry
classified by whether retrying can possibly help, L2 normalisation, the provider's own
token accounting, and org-scoped persistence into `chunks.embedding`.

What is not: search. Fusion, rerank and the ACL-filtered query of §12 are S7, and the
trap waiting there is already recorded — HNSW plus a restrictive ACL filter returns fewer
than `k`, and the fix is a larger `ef_search` and over-fetching, never a looser filter.

Every constant in `config.py` was measured against the live API rather than read from
documentation, and each records what was observed. Two of those measurements changed the
design: MRL-truncated vectors are not normalised, and over-long input is truncated
silently under HTTP 200.
"""

from jutsu_retrieval.client import EmbeddingTransport, VertexTransport, classify_status
from jutsu_retrieval.config import (
    DEFAULT_MAX_BATCH_SIZE,
    MEASURED_INPUT_TOKEN_LIMIT,
    EmbeddingSettings,
    MissingEmbeddingSettings,
    get_embedding_settings,
)
from jutsu_retrieval.embeddings import (
    Embedder,
    Embedding,
    EmbeddingTask,
    TokenLedger,
    embed_batch,
    l2_normalise,
    plan_batches,
)
from jutsu_retrieval.errors import (
    EmbeddingBudgetExceeded,
    EmbeddingError,
    PermanentEmbeddingError,
    TransientEmbeddingError,
    TruncatedInput,
)
from jutsu_retrieval.evidence import fetch_evidence
from jutsu_retrieval.persistence import (
    EmbeddingRun,
    PendingChunk,
    embed_pending_chunks,
    pending_chunks,
    store_embeddings,
)
from jutsu_retrieval.search import (
    ACL_PREDICATE,
    DEFAULT_EF_SEARCH_LADDER,
    DEFAULT_K,
    ORG_SCOPE_SQL,
    Evidence,
    SearchPage,
    SearchStats,
    search_chunks,
)

__all__ = [
    "ACL_PREDICATE",
    "DEFAULT_EF_SEARCH_LADDER",
    "DEFAULT_K",
    "DEFAULT_MAX_BATCH_SIZE",
    "MEASURED_INPUT_TOKEN_LIMIT",
    "ORG_SCOPE_SQL",
    "Embedder",
    "Embedding",
    "EmbeddingBudgetExceeded",
    "EmbeddingError",
    "EmbeddingRun",
    "EmbeddingSettings",
    "EmbeddingTask",
    "EmbeddingTransport",
    "Evidence",
    "MissingEmbeddingSettings",
    "PendingChunk",
    "PermanentEmbeddingError",
    "SearchPage",
    "SearchStats",
    "TokenLedger",
    "TransientEmbeddingError",
    "TruncatedInput",
    "VertexTransport",
    "classify_status",
    "embed_batch",
    "embed_pending_chunks",
    "fetch_evidence",
    "get_embedding_settings",
    "l2_normalise",
    "pending_chunks",
    "plan_batches",
    "search_chunks",
    "store_embeddings",
]
