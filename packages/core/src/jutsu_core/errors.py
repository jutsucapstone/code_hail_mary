"""Typed error hierarchy.

One envelope for every 4xx/5xx the gateway emits (§15), so the frontend has a single
shape to handle and never has to parse a message string.

Messages are safe to show a caller. Spec §4.9 forbids PII in logs, traces and error
messages, so nothing here interpolates document text, an email address or a person's
name — identifiers only.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AclDenied",
    "ExtractionRejected",
    "InsufficientEvidence",
    "JutsuError",
    "NotFound",
    "RateLimited",
    "ValidationFailed",
]


class JutsuError(Exception):
    """Base for every error the API converts into a response envelope."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def envelope(self, request_id: str) -> dict[str, Any]:
        """The single error shape all 4xx/5xx responses use."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
            "request_id": request_id,
        }


class ValidationFailed(JutsuError):
    status_code = 422
    code = "validation_failed"


class NotFound(JutsuError):
    status_code = 404
    code = "not_found"


class AclDenied(JutsuError):
    """The caller cannot see the evidence behind this fact.

    Deliberately indistinguishable from `NotFound` on the wire: §4.5 warns that leaking
    existence through status codes or result counts is the same leak as returning the
    content. The API maps this to 404.
    """

    status_code = 404
    code = "not_found"


class RateLimited(JutsuError):
    status_code = 429
    code = "rate_limited"


class InsufficientEvidence(JutsuError):
    """The grounding validator could not cite every claim (§4.3).

    Not an error condition in the usual sense — it is the correct, honest answer when
    the corpus does not support one. Returned as 200 with `insufficient_evidence: true`
    by the ask endpoint; this class exists for the internal path that raises it.
    """

    status_code = 200
    code = "insufficient_evidence"


class ExtractionRejected(JutsuError):
    """A claim failed the hallucination gate — its quote was not in the chunk (§4.2)."""

    status_code = 422
    code = "extraction_rejected"
