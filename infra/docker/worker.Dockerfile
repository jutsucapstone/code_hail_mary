# The arq worker, for Cloud Run.
#
# Deliberately the same build as api.Dockerfile with a different command. The worker
# imports `jutsu_db` and shares the whole dependency set, so a second base image would be
# two things to patch, two things to pin and two chances for them to drift apart on a
# security update.
#
# Runs as a Cloud Run *service* rather than a job, because arq's cron scheduler lives in
# the process: it has to stay up to fire `reap_expired_registrations` every five minutes.
# A job would run once and exit, and the reaper would never fire at all — which is the
# same failure as having no reaper, in a different costume. Deploy it with
# `--no-cpu-throttling` and `--min-instances=1`, or Cloud Run idles the container between
# requests and the scheduler stops with it.

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
