"""JUTSU domain core — models, enums and typed errors.

Pure types. No IO, no LLM calls, no database access; every other package depends on
this one, so it must stay import-cheap and side-effect free.
"""

from jutsu_core.errors import (
    AclDenied,
    Conflict,
    ExtractionRejected,
    InsufficientEvidence,
    InternalError,
    JutsuError,
    NotFound,
    PermissionDenied,
    RateLimited,
    ServiceUnavailable,
    Unauthenticated,
    ValidationFailed,
)
from jutsu_core.ids import (
    JutsuIdKind,
    generate_jutsu_id,
    is_valid_jutsu_id,
    normalise_jutsu_id,
)
from jutsu_core.models import (
    AclEntry,
    Chunk,
    Connector,
    MaskedSpan,
    MaskResult,
    PiiType,
    RawDocument,
    SourceSystem,
    acl_hash_of,
    content_hash_of,
)

__all__ = [
    "AclDenied",
    "AclEntry",
    "Chunk",
    "Conflict",
    "Connector",
    "ExtractionRejected",
    "InsufficientEvidence",
    "InternalError",
    "JutsuError",
    "JutsuIdKind",
    "MaskResult",
    "MaskedSpan",
    "NotFound",
    "PermissionDenied",
    "PiiType",
    "RateLimited",
    "RawDocument",
    "ServiceUnavailable",
    "SourceSystem",
    "Unauthenticated",
    "ValidationFailed",
    "acl_hash_of",
    "content_hash_of",
    "generate_jutsu_id",
    "is_valid_jutsu_id",
    "normalise_jutsu_id",
]
