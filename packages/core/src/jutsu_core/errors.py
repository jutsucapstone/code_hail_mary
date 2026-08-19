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
    "Conflict",
    "ExtractionRejected",
    "InsufficientEvidence",
    "JutsuError",
    "NotFound",
    "PermissionDenied",
    "RateLimited",
    "ServiceUnavailable",
    "Unauthenticated",
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


class Unauthenticated(JutsuError):
    """No usable session on a route that requires one.

    Distinct from `PermissionDenied`: this says "we do not know who you are", which the
    frontend answers by sending the caller to sign in. A caller we *do* know, who simply
    may not do this, must never be sent round the sign-in loop — that is a 403.
    """

    status_code = 401
    code = "unauthenticated"


class PermissionDenied(JutsuError):
    """Authenticated, inside the tenant, and lacking the permission.

    403 rather than 404 is correct *here* and only here. `AclDenied` returns 404 because
    the existence of a document is itself the secret. The existence of an admin screen is
    not: the caller already knows they are in this organisation, so hiding it would only
    make the product feel broken while protecting nothing.

    A cross-tenant reference is different again, and never reaches this class — row-level
    security returns zero rows, so it surfaces as `NotFound` without any code deciding to
    hide it.
    """

    status_code = 403
    code = "permission_denied"


class Conflict(JutsuError):
    """The request is well-formed but the current state refuses it.

    Duplicate organisation domain, an invitation that was already accepted, a JUTSU ID
    already claimed. Carries no detail about the conflicting row by default: on the
    registration path that detail is another tenant's existence.
    """

    status_code = 409
    code = "conflict"


class ServiceUnavailable(JutsuError):
    """A dependency this request needs is not answering.

    Used where failing CLOSED is the correct trade — rate limiting on an authentication
    endpoint, for instance, where an unreachable limiter must not silently become no
    limiter at all.
    """

    status_code = 503
    code = "service_unavailable"
