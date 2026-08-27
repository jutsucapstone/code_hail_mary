"""JUTSU gateway.

Stateless, request-path only (§6). Long-running work goes to the worker via the queue;
nothing here blocks on extraction or ingestion.

S0 ships the shape: liveness, readiness, the single error envelope and request-id
propagation. The `/v1` surface in §15 lands slice by slice from S7 onward.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from jutsu_core import InternalError, JutsuError, ValidationFailed

from jutsu_api.routers import auth as auth_router
from jutsu_api.routers import employees as employees_router
from jutsu_api.routers import evidence as evidence_router
from jutsu_api.routers import identities as identities_router
from jutsu_api.routers import me as me_router
from jutsu_api.routers import orgs as orgs_router
from jutsu_api.security import public

REQUEST_ID_HEADER: Final = "x-request-id"

logger = logging.getLogger("jutsu.api")


def _configure_logging() -> None:
    """Structured JSON to stdout.

    §4.9 forbids PII in logs. The formatter emits only the fields listed here, so a
    stray `logger.info(document.body)` cannot leak text through an unexpected attribute
    — the message itself is the caller's responsibility, but nothing is auto-attached.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter('{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def create_app() -> FastAPI:
    _configure_logging()

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
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(JutsuError)
    async def jutsu_error_handler(request: Request, exc: JutsuError) -> JSONResponse:
        """The one envelope for every 4xx/5xx (§15)."""
        request_id = getattr(request.state, "request_id", "unknown")
        logger.warning("%s", exc.code)
        return JSONResponse(status_code=exc.status_code, content=exc.envelope(request_id))

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
        logger.warning("validation_failed")
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
        """Readiness — whether dependencies are reachable.

        Reports `degraded` while there are no dependencies to check, rather than a bare
        `ok`: an unconditional 200 here would make the Cloud Run health gate meaningless
        the moment Postgres and Neo4j are wired in at S1/S2.
        """
        checks: dict[str, str] = {
            "postgres": "not_configured",
            "neo4j": "not_configured",
        }
        ready = all(v == "ok" for v in checks.values())
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

    return app


app = create_app()
