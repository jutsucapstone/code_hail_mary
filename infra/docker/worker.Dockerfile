# The worker image, for Cloud Run.
#
# Deliberately the same build as api.Dockerfile with a different command. The worker
# imports `jutsu_db` and shares the whole dependency set, so a second base image would be
# two things to patch, two things to pin and two chances for them to drift apart on a
# security update.
#
# Nothing deploys this as a long-running service today. It exists for two callers: the
# `jutsu-reap` Cloud Run *job*, which deploy.yml points at this image on every push and
# Cloud Scheduler invokes every five minutes with its own `--command` (so nothing runs
# or bills at idle), and the future ingestion-worker slice (S8). An always-on arq
# service means `--min-instances=1 --no-cpu-throttling` plus Redis — a standing cost
# that is that slice's own decision, not a default smuggled in from a Dockerfile
# header. The CMD below is what that service will run when it lands.

# ---------------------------------------------------------------- build
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

WORKDIR /repo

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

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

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-packages --no-install-workspace

COPY packages/ packages/
COPY apps/api/ apps/api/
COPY apps/worker/ apps/worker/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-packages

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime

RUN useradd --create-home --shell /usr/sbin/nologin --uid 1001 jutsu

WORKDIR /repo

ENV PATH="/repo/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=build --chown=jutsu:jutsu /repo /repo

USER jutsu

# `exec`, so arq is PID 1 and SIGTERM reaches it. arq drains in-flight jobs on that
# signal; a shell holding PID 1 would swallow it and the jobs would be killed mid-flight.
CMD ["sh", "-c", "exec arq jutsu_worker.main.WorkerSettings"]
