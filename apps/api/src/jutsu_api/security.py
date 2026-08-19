"""Server-side authorization. Every access decision in JUTSU is made here.

The frontend holds a session cookie and forwards it. It decides nothing. The cookie is an
opaque handle with no claims in it — no org id, no user id, no role — so there is nothing
for a Next route to branch on even if someone tried. Whatever the browser sends, the
answer to "who is this and what may they do" is computed in this module against the
database.

**Authorization is declared, and forgetting to declare it is a startup failure.**

The usual shape — a dependency you remember to add — fails open: a new route with no
guard is world-readable, and nothing complains. Here every route must carry either
`@requires(...)` or `@public(...)`, and `GuardedAPIRoute` raises `UndeclaredRoute` while
the router is being built. The app cannot start with an unguarded endpoint, so the
failure arrives at import time in CI rather than as a quiet disclosure in production.

`@public` is deliberately noisy and takes a written reason. Making the unsafe option
require an explanation is the point: a reviewer sees the sentence, not just the absence
of a decorator.

**Two layers, doing different jobs.** Row-level security scopes *rows* to an
organisation; this module decides what an actor may *do*. They are not substitutes. RLS
is why a cross-tenant reference surfaces as `NotFound` — the row is simply not there —
rather than as a permission decision some code had to remember to make.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, TypeVar
from uuid import UUID

from fastapi import Request
from fastapi.routing import APIRoute
from jutsu_core.errors import PermissionDenied, Unauthenticated
from jutsu_core.rbac import Permission, Role

__all__ = [
    "GuardedAPIRoute",
    "Principal",
    "UndeclaredRoute",
    "public",
    "requires",
    "session_token_hash",
]

#: Attribute stamped onto an endpoint by the decorators below. Read by GuardedAPIRoute.
_DECLARATION_ATTR: Final = "__jutsu_authz__"

SESSION_COOKIE: Final = "__Host-jutsu_session"
CSRF_COOKIE: Final = "__Host-jutsu_csrf"
CSRF_HEADER: Final = "x-jutsu-csrf"

#: Methods that may change state, and therefore need the double-submit CSRF check.
_UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})

F = TypeVar("F", bound=Callable[..., Any])


class UndeclaredRoute(RuntimeError):
    """A route was registered without declaring its authorization.

    Raised while the router is built, so this is a failure to start rather than a
    permissive default. If you are seeing it, add `@requires(Permission.X)` — or
    `@public("why this is safe")` if the endpoint genuinely needs no session.
    """


@dataclass(frozen=True, slots=True)
class _Declaration:
    permission: Permission | None
    public_reason: str | None


def requires(permission: Permission) -> Callable[[F], F]:
    """Declare the permission a route needs.

    A permission, never a role. A route naming a role has to be revisited every time the
    role list changes; a route naming a permission does not.
    """

    def decorate(endpoint: F) -> F:
        setattr(endpoint, _DECLARATION_ATTR, _Declaration(permission, None))
        return endpoint

    return decorate


def public(reason: str) -> Callable[[F], F]:
    """Declare that a route intentionally needs no session.

    The reason is required and is not decoration: sign-in, the health probes and the
    OAuth callback are the only shapes that legitimately qualify, and writing the
    sentence is what makes a wrong one obvious in review.
    """
    if not reason.strip():
        raise ValueError("public() requires a written reason")

    def decorate(endpoint: F) -> F:
        setattr(endpoint, _DECLARATION_ATTR, _Declaration(None, reason))
        return endpoint

    return decorate


def declaration_of(endpoint: object) -> _Declaration | None:
    """The declaration attached to an endpoint, or None if it never got one."""
    found = getattr(endpoint, _DECLARATION_ATTR, None)
    return found if isinstance(found, _Declaration) else None


class GuardedAPIRoute(APIRoute):
    """An APIRoute that refuses to exist without an authorization declaration.

    Set as `route_class` on every router. Because FastAPI constructs routes at import
    time, an undeclared endpoint takes the process down before it can serve anything.

    Known limit, stated rather than hidden: this sees endpoints registered through a
    router using this class. A route mounted directly on `app`, or a sub-application,
    bypasses it — so `test_every_route_is_declared` walks `app.routes` at runtime as
    well, and the two together are what actually close the hole.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        endpoint = kwargs.get("endpoint")
        if endpoint is None and len(args) > 1:
            endpoint = args[1]

        if endpoint is not None and declaration_of(endpoint) is None:
            path = kwargs.get("path") or (args[0] if args else "<unknown>")
            raise UndeclaredRoute(
                f"{path} ({getattr(endpoint, '__name__', endpoint)}) declares no "
                "authorization. Add @requires(Permission.X), or @public(reason) if it "
                "genuinely needs no session."
            )

        super().__init__(*args, **kwargs)


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, resolved server-side from an opaque session handle.

    Everything here came from the database. Nothing came from the cookie except the
    handle used to look it up, which is the property that makes the frontend unable to
    influence an access decision.
    """

    session_id: UUID
    identity_id: UUID
    user_id: UUID
    org_id: UUID
    role: Role
    permissions: frozenset[Permission]

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if not self.can(permission):
            # No detail about what would have been needed: the caller is inside the
            # tenant, so the *shape* of the permission model is not secret, but naming
            # the missing permission turns a denial into a map of the admin surface.
            raise PermissionDenied("You do not have permission to perform this action.")


def session_token_hash(token: str) -> bytes:
    """SHA-256 of a session token, which is what the database stores.

    The token is 256 bits of CSPRNG output, so a fast hash is the right choice: there is
    no dictionary to attack and no work factor worth paying on every request. Storing the
    token itself would mean a database read yields a usable credential.
    """
    return hashlib.sha256(token.encode("ascii")).digest()


def verify_csrf(request: Request, expected_hash: bytes) -> None:
    """Double-submit check on state-changing requests.

    The cookie is `SameSite=Lax`, which already blocks cross-site POSTs from a form, but
    Lax still sends the cookie on a top-level GET navigation — so it is not sufficient on
    its own for anything that changes state via a link. The paired non-HttpOnly cookie is
    readable by our own page and by nobody else's origin, so echoing it in a header
    proves same-origin.

    Deliberately not a header the proxy sets: a value invented by the server it is meant
    to protect proves nothing at all.
    """
    if request.method not in _UNSAFE_METHODS:
        return

    presented = request.headers.get(CSRF_HEADER, "")
    if not presented or not hmac.compare_digest(
        hashlib.sha256(presented.encode("ascii")).digest(), expected_hash
    ):
        raise Unauthenticated("Missing or invalid CSRF token.")


PrincipalResolver = Callable[[Request], Awaitable[Principal]]
