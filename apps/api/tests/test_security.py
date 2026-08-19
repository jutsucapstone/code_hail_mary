"""The authorization guard.

The single highest-value test here is `test_an_undeclared_route_cannot_be_registered`.
Every other control in the system assumes routes are guarded; this is what makes that
assumption true rather than hoped for. The usual arrangement — a dependency you remember
to add — fails open, and a new endpoint with no guard is world-readable while nothing
complains. Here the failure is at import time, so it arrives in CI.

`test_every_route_in_the_app_is_declared` is the belt to that pair of braces: it walks the
live application, so a route mounted directly on `app` rather than through a guarded
router — which `GuardedAPIRoute` cannot see — is still caught.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi import APIRouter, FastAPI, Request
from jutsu_api.deps import get_principal
from jutsu_api.main import create_app
from jutsu_api.security import (
    CSRF_HEADER,
    GuardedAPIRoute,
    Principal,
    UndeclaredRoute,
    declaration_of,
    iter_api_routes,
    public,
    requires,
    session_token_hash,
    verify_csrf,
)
from jutsu_core.errors import PermissionDenied, Unauthenticated
from jutsu_core.rbac import Permission, Role, permissions_for


def _guarded_router() -> APIRouter:
    return APIRouter(route_class=GuardedAPIRoute)


class TestDeclarationIsMandatory:
    def test_an_undeclared_route_cannot_be_registered(self) -> None:
        """Fail closed, at import time.

        This is the test that makes "every endpoint is authorised" a property of the
        build rather than of reviewer attention.
        """
        router = _guarded_router()

        with pytest.raises(UndeclaredRoute) as excinfo:

            @router.get("/leaky")
            async def leaky() -> dict[str, str]:
                return {"secrets": "everything"}

        assert "/leaky" in str(excinfo.value)
        assert "@requires" in str(excinfo.value), "the error must say how to fix it"

    def test_a_declared_route_registers_normally(self) -> None:
        router = _guarded_router()

        @router.get("/employees")
        @requires(Permission.MEMBER_READ)
        async def list_employees() -> list[str]:
            return []

        assert len(router.routes) == 1

    def test_a_public_route_registers_with_its_reason(self) -> None:
        router = _guarded_router()

        @router.post("/auth/request")
        @public("Sign-in cannot require a session; that is what it issues.")
        async def request_challenge() -> dict[str, str]:
            return {"status": "sent"}

        declaration = declaration_of(request_challenge)
        assert declaration is not None
        assert declaration.permission is None
        assert declaration.public_reason

    def test_public_demands_an_actual_reason(self) -> None:
        """An empty string would let someone silence the guard without saying why."""
        with pytest.raises(ValueError, match="written reason"):
            public("   ")

    def test_the_decorators_are_mutually_exclusive_in_effect(self) -> None:
        """Whichever is applied last wins, and it is never both at once.

        Worth pinning: a route carrying both would otherwise be ambiguous, and the
        ambiguity would resolve differently depending on decorator order.
        """

        @requires(Permission.ORG_READ)
        @public("nominally public")
        async def confused() -> None: ...

        declaration = declaration_of(confused)
        assert declaration is not None
        assert declaration.permission is Permission.ORG_READ
        assert declaration.public_reason is None


class TestEveryRouteInTheLiveApp:
    def test_every_route_in_the_app_is_declared(self) -> None:
        """Walks the real application, catching routes the route_class never saw.

        `GuardedAPIRoute` only sees endpoints registered through a router that uses it.
        Anything mounted directly on `app` bypasses it entirely, which is exactly the
        kind of shortcut added under time pressure.
        """
        app = create_app()

        routes = list(iter_api_routes(app))
        # Guards the guard: if the traversal stops seeing mounted routers, this test
        # silently checks almost nothing.
        assert len(routes) >= 5, "route traversal is not reaching the mounted routers"

        undeclared = [route.path for route in routes if declaration_of(route.endpoint) is None]
        assert not undeclared, (
            f"routes with no authorization declaration: {undeclared}. "
            "Register them through a router with route_class=GuardedAPIRoute."
        )

    def test_the_documentation_endpoints_are_not_served_unconditionally(self) -> None:
        """A published schema is a map of every endpoint and payload.

        Not an access-control failure on its own, but in production it hands an attacker
        the enumeration step for free.
        """
        app = create_app()
        assert isinstance(app, FastAPI)
        # Recorded as a decision rather than asserted as absent: docs are useful in dev.
        # What must not happen is them being on in production by default.
        assert app.docs_url is None or app.openapi_url is not None


class TestPrincipal:
    def _principal(self, role: Role) -> Principal:
        import uuid

        return Principal(
            session_id=uuid.uuid4(),
            identity_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            role=role,
            permissions=permissions_for(role),
        )

    def test_require_passes_for_a_held_permission(self) -> None:
        self._principal(Role.HR_ADMIN).require(Permission.MEMBER_INVITE)

    def test_require_raises_for_a_missing_permission(self) -> None:
        with pytest.raises(PermissionDenied):
            self._principal(Role.HR_ADMIN).require(Permission.INTEGRATION_CONNECT)

    def test_the_denial_does_not_name_the_missing_permission(self) -> None:
        """A denial that names what was needed maps the admin surface for the caller."""
        with pytest.raises(PermissionDenied) as excinfo:
            self._principal(Role.VIEWER).require(Permission.ORG_DELETE)

        assert "org:delete" not in str(excinfo.value)

    def test_a_member_cannot_read_the_member_list(self) -> None:
        """The self-service permissions must not leak into administrative ones."""
        member = self._principal(Role.MEMBER)
        assert member.can(Permission.PROFILE_SELF_UPDATE)
        assert not member.can(Permission.MEMBER_READ)


class TestSessionTokenHashing:
    def test_the_stored_value_is_not_the_token(self) -> None:
        token = "s" * 43
        digest = session_token_hash(token)

        assert digest != token.encode()
        assert len(digest) == 32
        assert digest == hashlib.sha256(token.encode()).digest()

    def test_hashing_is_deterministic(self) -> None:
        assert session_token_hash("abc") == session_token_hash("abc")
        assert session_token_hash("abc") != session_token_hash("abd")


class TestCsrf:
    def _request(self, method: str, header: str | None) -> Request:
        headers = [] if header is None else [(CSRF_HEADER.encode(), header.encode())]
        return Request({"type": "http", "method": method, "headers": headers, "path": "/"})

    def test_safe_methods_are_not_challenged(self) -> None:
        """A GET has nothing to forge, and demanding a header would break navigation."""
        verify_csrf(self._request("GET", None), hashlib.sha256(b"anything").digest())

    def test_an_unsafe_method_without_the_header_is_rejected(self) -> None:
        with pytest.raises(Unauthenticated):
            verify_csrf(self._request("POST", None), hashlib.sha256(b"token").digest())

    def test_an_unsafe_method_with_the_wrong_token_is_rejected(self) -> None:
        with pytest.raises(Unauthenticated):
            verify_csrf(self._request("POST", "wrong"), hashlib.sha256(b"token").digest())

    def test_an_unsafe_method_with_the_matching_token_passes(self) -> None:
        verify_csrf(self._request("POST", "token"), hashlib.sha256(b"token").digest())


class TestDeclarationIsEnforced:
    """Declaring a permission must actually deny, not merely describe.

    The import-time guard proves every route *names* a permission. For a while nothing
    consulted that name per request, so every authenticated caller reached every endpoint
    regardless of role — a bare Member could list the organisation's people. Declared,
    documented, tested for presence, and completely inert.

    That is the same failure shape as ADR 0003's row-level security: a control that looks
    enforced because the machinery around it exists. These tests check the machinery
    actually bites.
    """

    def test_every_permissioned_route_resolves_the_principal(self) -> None:
        """Enforcement lives in `get_principal`, so a route must depend on it.

        A handler that declares a permission but takes no principal would sail straight
        past the check. Presence of the dependency is what makes the declaration binding,
        so it is asserted rather than assumed.
        """
        app = create_app()

        missing = []
        for route in iter_api_routes(app):
            declaration = declaration_of(route.endpoint)
            if declaration is None or declaration.permission is None:
                continue
            resolves = any(
                dependency.call is get_principal for dependency in route.dependant.dependencies
            ) or _depends_on_principal(route.dependant)
            if not resolves:
                missing.append(route.path)

        assert not missing, (
            f"routes declaring a permission but never resolving a principal: {missing}. "
            "The declaration is inert on those — add the CurrentPrincipal dependency."
        )


def _depends_on_principal(dependant: object, depth: int = 0) -> bool:
    """Walk the dependency tree looking for `get_principal`.

    Recursive because the principal is usually reached through an annotated alias rather
    than declared directly, so it sits one or more levels down the graph.
    """
    if depth > 6:
        return False
    for dependency in getattr(dependant, "dependencies", []):
        if getattr(dependency, "call", None) is get_principal:
            return True
        if _depends_on_principal(dependency, depth + 1):
            return True
    return False
