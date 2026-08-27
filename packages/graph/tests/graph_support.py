"""Fixtures for the graph suite.

**Deliberately not `conftest.py`.** `packages/db/tests/conftest.py` already exists, mypy
derives module names from the nearest directory without an `__init__.py`, and a second
`conftest` under the `packages` tree makes `mypy packages` ambiguous — at which point it
refuses to check anything at all and the typecheck step is green having read nothing. The
Makefile records this; the cost of ignoring it is a silently disabled gate.

So fixtures live here and each test module imports the ones it needs.

These tests run against a **real Neo4j**. There is no in-memory stand-in: constraints,
fulltext indexes, transaction timeouts and the bitemporal comparisons are the things
under test, and a fake would pass while proving none of them.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from jutsu_graph.driver import GraphSettings, close_driver, get_graph_settings, write_session

GRAPH_REACHABLE_ENV = "JUTSU_GRAPH_REACHABLE"


def skip_without_graph() -> None:
    """Skip unless something is really listening.

    The root conftest probes once and publishes the answer, exactly as it does for
    Postgres. "Is NEO4J_URI set" is the wrong question now that `.env` is loaded during
    collection — the variable is always set, so a stopped container would turn a clean
    skip into a wall of connection errors that also blocks every commit, because the
    pre-commit hook runs preflight.
    """
    if os.environ.get(GRAPH_REACHABLE_ENV) != "1":
        pytest.skip("nothing listening at NEO4J_URI — start Neo4j with `make up`")


@dataclass(frozen=True, slots=True)
class GraphFixture:
    """Connection details plus two organisation ids that do not exist anywhere else."""

    settings: GraphSettings
    org_a: uuid.UUID
    org_b: uuid.UUID


async def purge(settings: GraphSettings, org_id: uuid.UUID) -> None:
    """Remove everything belonging to one organisation.

    Goes through `write_session`, not a raw driver call, so teardown is subject to the
    same scoping rule as the code under test. A cleanup routine that could reach across
    tenants would be the one piece of the suite able to prove the opposite of what the
    suite asserts.
    """
    async with write_session(org_id, settings=settings) as session:
        await session.run("MATCH (n) WHERE n.org_id = $org_id DETACH DELETE n")


# `name=` and a differently-named function, deliberately. A fixture imported into a
# test module under its own name is then shadowed by the test parameter of the same
# name, which every linter reads as a redefinition — fifteen `noqa: F811` in one file.
# Naming the function `graph_fixture` and the fixture `graph` means the import and the
# parameter never collide.
@pytest.fixture(name="graph")
async def graph_fixture() -> AsyncIterator[GraphFixture]:
    """A live graph, two fresh organisation ids, and cleanup afterwards.

    The ids are random per test rather than fixed, which means tests are isolated from
    each other without wiping the database — and it means every test is also, incidentally,
    running against a store that contains other tenants' data. That is the condition the
    tenancy assertions need in order to mean anything.

    The driver is closed at the end of each test. It is a process-wide singleton, so
    leaving it open would leak a connection pool between tests and make a failure in one
    surface as a confusing error in the next.
    """
    skip_without_graph()
    settings = get_graph_settings()
    fixture = GraphFixture(settings=settings, org_a=uuid.uuid4(), org_b=uuid.uuid4())
    try:
        yield fixture
    finally:
        await purge(settings, fixture.org_a)
        await purge(settings, fixture.org_b)
        await close_driver()


@pytest.fixture(name="graph_settings")
async def graph_settings_fixture() -> AsyncIterator[GraphSettings]:
    """Connection details only, for tests that manage their own sessions."""
    skip_without_graph()
    try:
        yield get_graph_settings()
    finally:
        await close_driver()
