"""Request-scoped dependencies.

One transaction per request, opened here and committed or rolled back by FastAPI's
teardown. Handlers never manage it, so a route cannot half-commit a multi-step operation
like registration.

The database session is opened WITHOUT an organisation scope, because at the moment a
request arrives we do not know which tenant it belongs to — that is derived from the
session handle. `resolve_principal` sets the GUC as soon as it knows, inside the same
transaction, and every statement after that point is filtered by row-level security.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from jutsu_core.errors import Unauthenticated
from jutsu_db.engine import get_sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession

from jutsu_api.auth_service import load_csrf_hash, resolve_principal
from jutsu_api.config import Settings, get_settings
from jutsu_api.email import ConsoleEmailSender, EmailSender
from jutsu_api.security import (
    SESSION_COOKIE,
    Principal,
    declaration_of,
    verify_csrf,
)

__all__ = [
    "CurrentPrincipal",
    "Db",
    "SettingsDep",
    "get_db",
    "get_email_sender",
    "get_principal",
]


async def get_db() -> AsyncIterator[AsyncSession]:
    factory = get_sessionmaker()
    async with factory() as session, session.begin():
        yield session


Db = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_email_sender(settings: SettingsDep) -> EmailSender:
    """The transport for outbound mail.

    Development gets the console. There is no production implementation yet, and
    `ConsoleEmailSender` refuses to construct outside development rather than silently
    discarding messages — a sender that swallows mail would make every sign-in fail with
    no error recorded anywhere, which is the worst failure shape an auth system can have.
    """
    return ConsoleEmailSender(environment=settings.environment)


async def get_principal(request: Request, session: Db) -> Principal:
    """The authenticated caller, or a refusal.

    Reads only the opaque handle from the cookie. Everything else — organisation, user,
    role, permissions — is resolved server-side, which is what makes it impossible for
    the browser to influence an authorisation outcome.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise Unauthenticated("Sign in to continue.")

    principal = await resolve_principal(session, token=token)

    # Checked after the session resolves, because the expected value lives on the session
    # row. Safe methods are exempt inside `verify_csrf`, so a GET pays nothing for this.
    csrf_hash = await load_csrf_hash(session, token=token)
    if csrf_hash is not None:
        verify_csrf(request, csrf_hash)

    # ENFORCE the route's declaration. This line is the difference between authorization
    # and the appearance of it.
    #
    # `@requires(...)` stamps an attribute that `GuardedAPIRoute` reads at import time, so
    # a route with no declaration cannot start. But that check only proves a permission
    # was *named* — for a while nothing consulted it per request, and every authenticated
    # caller could reach every endpoint regardless of role. A bare Member could list the
    # organisation's people. Declared, documented, tested for presence, and completely
    # inert: the same shape as the row-level security failure ADR 0003 records.
    #
    # The declaration is read from the matched route rather than passed in, so a handler
    # cannot accidentally check a different permission from the one it advertises — the
    # OpenAPI description and the enforcement come from one source.
    route = request.scope.get("route")
    declaration = declaration_of(getattr(route, "endpoint", None))
    if declaration is not None and declaration.permission is not None:
        principal.require(declaration.permission)

    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
