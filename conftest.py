"""Load `.env` before collection, so the database-backed suite actually runs.

Without this, `make preflight` passes while 112 tests quietly skip. Every fixture that
needs Postgres is guarded by `pytest.mark.skipif(not os.environ.get(...))`, and pytest
does not read `.env` — so unless the developer happened to export the variables into
their shell first, the RLS suite, the tenancy suite and the whole registration flow
reported green having executed nothing. That is a worse failure than a red build: the
gate says the isolation properties hold, and it never checked them.

Found the hard way. The variables were sitting in `.env` and the suite still skipped.

**Existing environment always wins.** CI sets these itself and points them at its own
service container; a file quietly overriding that would mean CI tested a database nobody
configured. This only fills gaps.

Deliberately no `python-dotenv`: parsing `KEY=value` is a dozen lines, and the stack is
fixed (CLAUDE.md). Quotes are stripped because `.env.example` does not use them but
people reasonably add them; `export ` prefixes are tolerated for the same reason.
"""

from __future__ import annotations

import os
import socket
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit


def _load_env_file() -> None:
    env_file = Path(__file__).parent / ".env"
    if not env_file.is_file():
        return

    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        key, separator, value = line.partition("=")
        if not separator:
            continue

        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        # setdefault, not assignment: a value already in the environment was put there
        # on purpose and outranks the file.
        os.environ.setdefault(key, value)


_load_env_file()


#: Set to "1" or "0" below, and read by each suite's `skipif`.
#:
#: An environment variable rather than an import, because the alternative is every test
#: package importing this module — which pytest only makes possible by accident of
#: `sys.path`, and which mypy checks as a separate root. One probe, one answer, no import
#: machinery, and each suite still states its own reason for skipping.
DB_REACHABLE_ENV = "JUTSU_DB_REACHABLE"


@lru_cache(maxsize=8)
def database_is_reachable(url: str | None) -> bool:
    """Whether something is actually listening where `url` points.

    The suite used to skip on "is the variable set", which was the same question right up
    until `.env` started being loaded above — after which the variable is *always* set,
    and a stopped Postgres turned a clean skip into a hundred connection errors that also
    blocked every commit, because the pre-commit hook runs preflight.

    So the guard now asks the question it actually meant. A TCP connect is enough: this
    only decides whether to run the tests, and the tests themselves report anything
    subtler. One second, because it is a local container or a service on the same
    network, and cached because it is asked once per test module.
    """
    if not url:
        return False

    # SQLAlchemy URLs carry a `+driver` suffix that `urlsplit` leaves in the scheme; the
    # host and port parse the same either way.
    parts = urlsplit(url)
    if not parts.hostname:
        return False

    try:
        with socket.create_connection((parts.hostname, parts.port or 5432), timeout=1.0):
            return True
    except OSError:
        return False


os.environ[DB_REACHABLE_ENV] = (
    "1" if database_is_reachable(os.environ.get("JUTSU_TEST_DATABASE_URL")) else "0"
)


#: The same question, asked of Neo4j. Set to "1" or "0" and read by the graph suite.
#:
#: A second variable rather than one combined flag: Postgres and Neo4j fail independently
#: — `make up` can bring one healthy and leave the other still starting — and a single
#: flag would skip a suite that could have run, or worse, run one that could not.
GRAPH_REACHABLE_ENV = "JUTSU_GRAPH_REACHABLE"

#: Bolt's default. Used when NEO4J_URI names no port.
_DEFAULT_BOLT_PORT = 7687


@lru_cache(maxsize=4)
def graph_is_reachable(uri: str | None) -> bool:
    """Whether something is listening where NEO4J_URI points.

    A TCP connect, exactly like the Postgres probe: this only decides whether to run the
    suite, and the tests themselves report anything subtler. Opening a real driver here
    would mean authenticating during collection, which turns a stopped container into a
    slow import rather than a clean skip.
    """
    if not uri:
        return False

    # `bolt://`, `neo4j://`, and their +s / +ssc variants all parse the same way.
    parts = urlsplit(uri)
    if not parts.hostname:
        return False

    try:
        with socket.create_connection(
            (parts.hostname, parts.port or _DEFAULT_BOLT_PORT), timeout=1.0
        ):
            return True
    except OSError:
        return False


os.environ[GRAPH_REACHABLE_ENV] = "1" if graph_is_reachable(os.environ.get("NEO4J_URI")) else "0"
