"""JUTSU gateway.

Stateless, request-path only (§6). Long-running work goes to the worker via the queue;
nothing here blocks on extraction or ingestion.

S0 ships the shape: liveness, readiness, the single error envelope and request-id
propagation. The `/v1` surface in §15 lands slice by slice from S7 onward.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from jutsu_core import InternalError, JutsuError, RateLimited, ValidationFailed
from jutsu_core.logs import configure as configure_logging
from jutsu_db.engine import ping as postgres_ping
from starlette.exceptions import HTTPException as StarletteHTTPException

from jutsu_api.logging_context import FIELDS, RequestContextFilter, bind, clear
from jutsu_api.queue import transport as doorbell_transport
from jutsu_api.routers import auth as auth_router
from jutsu_api.routers import connections as connections_router
from jutsu_api.routers import employees as employees_router
from jutsu_api.routers import evidence as evidence_router
from jutsu_api.routers import identities as identities_router
from jutsu_api.routers import kt as kt_router
from jutsu_api.routers import kt_console as kt_console_router
from jutsu_api.routers import me as me_router
from jutsu_api.routers import operations as operations_router
from jutsu_api.routers import orgs as orgs_router
from jutsu_api.routers import roles as roles_router
from jutsu_api.routers import search as search_router
from jutsu_api.security import public

REQUEST_ID_HEADER: Final = "x-request-id"

logger = logging.getLogger("jutsu.api")


def _configure_logging() -> None:
    """Structured JSON to stdout.

    §4.9 forbids PII in logs. The formatter emits only the fields listed here, so a
    stray `logger.info(document.body)` cannot leak text through an unexpected attribute
    — the message itself is the caller's responsibility, but nothing is auto-attached.

    The threshold comes from `LOG_LEVEL` (deploy.yml sets it; `.env.example` documents
    it), matched case-insensitively against logging's own level names. Unset or
    unrecognised falls back to INFO rather than raising — a typo in an env var must not
    take the service down, and must not silence it either.
    """
    # The filter stamps request_id / org_id / user_id (opaque ids, never PII) from the
    # per-request context onto every record, whichever module emitted it, and the
    # formatter names them — see `logging_context`. Unbound fields render as "-".
    #
    # The formatter is shared with the worker (`jutsu_core.logs`) because both had the
    # same two defects: a format string cannot escape the message it interpolates, so a
    # quote in a message produced a line that is not JSON; and uvicorn's own loggers do
    # not propagate, so its tracebacks went out as plain text beside the JSON stream.
    configure_logging(context_fields=FIELDS, filters=(RequestContextFilter(),))


def create_app() -> FastAPI:
    _configure_logging()
    # Which doorbell this process rings (ADR 0017). Resolved here, so a partial Cloud
    # Tasks configuration raises before the app exists and a mis-deployed API fails to
    # start — the deploy's readiness check fails with it — rather than enqueueing rows
    # nobody drains.
    transport = doorbell_transport()

    # Announced on startup rather than at construction, because `create_app` also runs
    # inside `scripts/emit-openapi.py`, which writes the schema to stdout — and so does
    # this logger. A line emitted here lands *in* `openapi.json` and makes it invalid
    # JSON, which `make api-types-check`, and therefore the commit gate, fails on.
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info("%s", {"event": "doorbell_transport", "transport": transport})
        yield

    # The interactive docs and the schema are a complete map of every endpoint and
    # payload. Useful in development, and free enumeration for an attacker in
    # production, so they are served only outside it.
    expose_schema = os.environ.get("JUTSU_ENV", "dev") != "prod"

    app = FastAPI(
        title="JUTSU API",
        version="0.1.0",
        description="Enterprise Memory OS gateway",
        docs_url="/docs" if expose_schema else None,
        redoc_url=None,
        openapi_url="/openapi.json" if expose_schema else None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        """Every response carries a request_id (§15).

        Honours an inbound header so a trace survives across services rather than
        restarting at each hop.
        """
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        # Fresh context for this task, then the id: every log line the request emits
        # carries it, and `get_principal` adds the org and user ids once it knows them.
        clear()
        bind(request_id=request_id)
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(JutsuError)
    async def jutsu_error_handler(request: Request, exc: JutsuError) -> JSONResponse:
        """The one envelope for every 4xx/5xx (§15).

        A 429 also carries `Retry-After`. Every budget in `rate_limit.py` is a fixed
        window and names its `window_seconds` in the envelope's details; the header is
        the same number spelled the way a client library already understands, set here
        once rather than in each route that spends a budget.
        """
        request_id = getattr(request.state, "request_id", "unknown")
        # The level follows the status, and the payload is a dict so the code and the
        # status are queryable fields rather than a bare string in `message`. A 500 and
        # a 404 logged identically at WARNING means "the database is down" and "somebody
        # typed a bad URL" are the same line to every alert built on severity.
        log = logger.error if exc.status_code >= 500 else logger.warning
        log(
            "%s",
            {
                "event": "request_failed",
                "code": exc.code,
                "status": exc.status_code,
                "path": request.url.path,
                "method": request.method,
            },
        )
        headers: dict[str, str] = {}
        if isinstance(exc, RateLimited):
            window = exc.details.get("window_seconds")
            if isinstance(window, int) and window > 0:
                headers["retry-after"] = str(window)
        return JSONResponse(
            status_code=exc.status_code, content=exc.envelope(request_id), headers=headers
        )

    #: What the router answers for a path or a method it never matched. Written out
    #: rather than reusing a `JutsuError` subclass because these are not application
    #: failures — nothing refused the caller, there was simply nothing there — and
    #: giving them a domain error class would invite a route to raise one.
    _ROUTER_CODES: Final[dict[int, tuple[str, str]]] = {
        404: ("not_found", "That endpoint does not exist."),
        405: ("method_not_allowed", "That endpoint does not accept this method."),
    }

    @app.exception_handler(StarletteHTTPException)
    async def router_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """A path or method the router never matched, in the one envelope (§15).

        Starlette answers these itself with `{"detail": ...}`, which is a second error
        shape that carries no `request_id` — so the failure a client hits most often, a
        typo'd path or a wrong verb, is the one it cannot parse with the code path it
        uses for every other error, and the one nobody can correlate to a log line.

        `exc.headers` is forwarded deliberately: a 405 carries `Allow`, and dropping it
        turns a correct refusal into an uninformative one.

        `exc.detail` is never forwarded. It is Starlette's own English for the status,
        and for a `HTTPException` raised elsewhere it could carry text this handler has
        not vetted — the sentence is written here instead.
        """
        request_id = getattr(request.state, "request_id", "unknown")
        code, message = _ROUTER_CODES.get(
            exc.status_code, ("request_failed", "That request could not be completed.")
        )
        logger.warning(
            "%s",
            {
                "event": "router_refused",
                "code": code,
                "status": exc.status_code,
                "path": request.url.path,
                "method": request.method,
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {"code": code, "message": message, "details": {}},
                "request_id": request_id,
            },
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Rejected input, in the one envelope, with the values stripped out.

        FastAPI default is to return {"detail": [...]} with an "input" key holding the
        value that failed. Two problems, both real. It is not the §15 envelope, so a
        client needs a second code path for exactly the responses it is most likely to
        hit. And it reflects the submitted value: on /v1/auth/request that is an email
        address echoed straight back to whoever posted it, which §4.9 forbids.

        Only the field location and the rule that failed are returned. That is what a
        form needs to mark the right input; the value is already in the caller hands.
        """
        request_id = getattr(request.state, "request_id", "unknown")
        fields = [
            {
                "field": ".".join(str(part) for part in error["loc"][1:]) or "body",
                "rule": error["type"],
            }
            for error in exc.errors()
        ]
        error = ValidationFailed("Some of the details you entered are not valid.")
        envelope = error.envelope(request_id)
        envelope["error"]["details"] = {"fields": fields}
        # Field names and rule ids only — never `input`, which is the submitted value.
        logger.warning(
            "%s",
            {
                "event": "validation_failed",
                "path": request.url.path,
                "method": request.method,
                "fields": [field["field"] for field in fields],
            },
        )
        return JSONResponse(status_code=error.status_code, content=envelope)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Anything nobody anticipated, still in the one envelope (§15).

        Without this, an unexpected exception falls through to Starlette's plain-text
        "Internal Server Error": not the envelope, and — the part that actually costs
        time — carrying no `request_id`. Someone reporting "it broke" then has nothing to
        quote, and there is no way to find their request among the logs.

        Found by running the built image with no database attached: `/v1/orgs/register`
        answered `Internal Server Error` as bare text while every other failure on the
        service answers as JSON.

        `exc_info` goes to the log and never to the client. A stack trace in a response
        names internal paths and library versions, and on this service it could carry a
        connection string; the caller gets the id and nothing else (§4.9).
        """
        request_id = getattr(request.state, "request_id", "unknown")
        logger.exception("unhandled_error", extra={"request_id": request_id})
        error = InternalError("Something went wrong on our side.")
        return JSONResponse(
            status_code=error.status_code,
            content=error.envelope(request_id),
            # Set here, not left to `request_id_middleware`. Starlette handles `Exception`
            # in `ServerErrorMiddleware`, which sits *outside* user middleware — so a 500
            # never passes back through the middleware that attaches this header, and the
            # one response a caller most needs to trace was the only one without it.
            headers={REQUEST_ID_HEADER: request_id},
        )

    @app.get("/healthz", tags=["ops"])
    @public("Liveness must answer before, and independently of, any session machinery.")
    async def healthz(request: Request) -> dict[str, Any]:
        """Liveness. Answers whether the process is up, nothing more."""
        return {"status": "ok", "request_id": getattr(request.state, "request_id", "unknown")}

    @app.get("/readyz", tags=["ops"])
    @public("Readiness is polled by the platform, which holds no session.")
    async def readyz(request: Request) -> dict[str, Any]:
        """Readiness — whether dependencies are actually reachable.

        Postgres is probed for real: `jutsu_db.engine.ping()` opens an unscoped session
        and runs `SELECT 1`, so "ok" means a connection was made and answered, not that a
        URL is set. Neo4j stays `not_configured` honestly — the gateway takes no
        dependency on `jutsu-graph` yet, and reporting a store this process never opens
        would be a health check describing somebody else's health.

        `ready` means **no probed dependency failed**. A `not_configured` entry is
        reported but does not block readiness: it is a statement that this deployment
        does not use the dependency, which is not an outage.
        """
        checks: dict[str, str] = {
            "postgres": "ok" if await postgres_ping() else "failed",
            "neo4j": "not_configured",
        }
        ready = all(v != "failed" for v in checks.values())
        return {
            "status": "ready" if ready else "degraded",
            "checks": checks,
            "request_id": getattr(request.state, "request_id", "unknown"),
        }

    app.include_router(auth_router.router)
    app.include_router(orgs_router.router)
    app.include_router(me_router.router)
    app.include_router(employees_router.router)
    app.include_router(identities_router.router)
    app.include_router(evidence_router.router)
    app.include_router(search_router.router)
    app.include_router(operations_router.router)
    app.include_router(connections_router.router)
    app.include_router(kt_router.router)
    app.include_router(kt_console_router.router)
    app.include_router(roles_router.router)

    return app


app = create_app()
