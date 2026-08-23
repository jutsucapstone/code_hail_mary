# The FastAPI gateway, for Cloud Run.
#
# Built from the repo root, because `jutsu-api` depends on `jutsu-core` and `jutsu-db` as
# uv workspace members. A context rooted at `apps/api` would not contain them.
#
# The same image serves the worker (see worker.Dockerfile, which only changes the command
# and the installed package): one Python base, built once, is less to keep in step than
# two that drift.

# ---------------------------------------------------------------- build
FROM python:3.12-slim AS build

# uv rather than pip: the lockfile is uv's, and resolving with anything else would install
# a set of versions nobody tested. Pinned by digest-stable tag so a CI run months from now
# builds what this one built.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

WORKDIR /repo

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Manifests first, so the dependency layer survives a source-only change.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY packages/db/pyproject.toml packages/db/
COPY packages/graph/pyproject.toml packages/graph/
COPY packages/retrieval/pyproject.toml packages/retrieval/
COPY packages/agents/pyproject.toml packages/agents/
COPY packages/connectors/pyproject.toml packages/connectors/
COPY packages/evals/pyproject.toml packages/evals/
COPY apps/api/pyproject.toml apps/api/
COPY apps/worker/pyproject.toml apps/worker/

# `--no-install-workspace` installs only third-party dependencies here. The workspace
# members are installed after the source is copied, so editing our own code does not
# invalidate the layer holding several hundred megabytes of wheels.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace

COPY packages/ packages/
COPY apps/api/ apps/api/
COPY apps/worker/ apps/worker/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime

# Not root, and no shell for the service account. Cloud Run will run whatever it is
# given; the constraint has to come from the image.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 1001 jutsu

WORKDIR /repo

ENV PATH="/repo/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=build --chown=jutsu:jutsu /repo /repo

USER jutsu

# Cloud Run injects PORT and it is not guaranteed to be 8080. Binding 0.0.0.0 matters:
# the default localhost bind is unreachable from outside the container, and it presents
# as a healthy container that answers nothing.
ENV PORT=8080
EXPOSE 8080

# A shell is needed to expand ${PORT}, and `exec` is what stops that shell from staying
# PID 1: it replaces itself with uvicorn, so SIGTERM reaches the server directly. Without
# it the shell swallows the signal, in-flight requests are never drained, and Cloud Run
# hard-kills the container at the end of the grace period.
CMD ["sh", "-c", "exec uvicorn jutsu_api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
