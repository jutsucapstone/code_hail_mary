"""Tenant isolation, read-only default, and secret handling (§4.7, §4.9, §17).

Neo4j has no row-level security. Everything Postgres gets from a policy the database
enforces, this layer has to get from `GraphSession.run` refusing to issue the query — so
these are the tests that stand where `test_rls.py` stands for `packages/db`, and they are
adversarial for the same reason.

The organisation ids are random per test, so every assertion below runs against a store
that genuinely contains other tenants' nodes. An isolation test against an empty database
passes whatever the code does.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from graph_support import (  # noqa: F401 — imported so pytest registers the fixtures
    GraphFixture,
    graph_fixture,
    graph_settings_fixture,
)
from jutsu_graph.driver import (
    GraphSettings,
    UnscopedQuery,
    WriteInReadSession,
    get_graph_settings,
    read_session,
    write_session,
)


async def seed_topic(settings: GraphSettings, org_id: uuid.UUID, name: str) -> None:
    async with write_session(org_id, settings=settings) as session:
        await session.run(
            "CREATE (t:Topic {org_id: $org_id, name: $name, id: $id})",
            name=name,
            id=str(uuid.uuid4()),
        )


class TestCrossTenantIsolation:
    async def test_a_scoped_read_sees_only_its_own_organisation(self, graph: GraphFixture) -> None:
        await seed_topic(graph.settings, graph.org_a, "alpha-only")
        await seed_topic(graph.settings, graph.org_b, "bravo-only")

        async with read_session(graph.org_a, settings=graph.settings) as session:
            rows = await session.run(
                "MATCH (t:Topic) WHERE t.org_id = $org_id RETURN t.name AS name"
            )

        names = {row["name"] for row in rows}
        assert names == {"alpha-only"}
        assert "bravo-only" not in names

    async def test_the_same_query_under_the_other_scope_returns_the_other_row(
        self, graph: GraphFixture
    ) -> None:
        """Disjoint results from one query text — the property, not just an empty set.

        A test that only asserted "org A cannot see B" would pass against code that
        returned nothing to anybody.
        """
        await seed_topic(graph.settings, graph.org_a, "alpha-only")
        await seed_topic(graph.settings, graph.org_b, "bravo-only")

        query = "MATCH (t:Topic) WHERE t.org_id = $org_id RETURN t.name AS name"
        async with read_session(graph.org_a, settings=graph.settings) as session:
            alpha = {row["name"] for row in await session.run(query)}
        async with read_session(graph.org_b, settings=graph.settings) as session:
            bravo = {row["name"] for row in await session.run(query)}

        assert alpha == {"alpha-only"}
        assert bravo == {"bravo-only"}
        assert alpha.isdisjoint(bravo)

    async def test_a_write_lands_only_in_its_own_organisation(self, graph: GraphFixture) -> None:
        await seed_topic(graph.settings, graph.org_a, "written-by-a")

        async with read_session(graph.org_b, settings=graph.settings) as session:
            rows = await session.run(
                "MATCH (t:Topic) WHERE t.org_id = $org_id AND t.name = $name RETURN t",
                name="written-by-a",
            )
        assert rows == []


class TestUnscopedQueriesAreRefused:
    async def test_a_query_without_org_id_raises(self, graph: GraphFixture) -> None:
        """The whole mechanism, in one assertion.

        In Postgres this query returns nothing, because RLS fails closed. Here it would
        return every tenant's data, so it must not run at all.
        """
        async with read_session(graph.org_a, settings=graph.settings) as session:
            with pytest.raises(UnscopedQuery, match="does not reference"):
                await session.run("MATCH (t:Topic) RETURN t.name AS name")

    async def test_an_unscoped_write_raises(self, graph: GraphFixture) -> None:
        async with write_session(graph.org_a, settings=graph.settings) as session:
            with pytest.raises(UnscopedQuery):
                await session.run("CREATE (t:Topic {name: 'smuggled'})")

    async def test_supplying_org_id_as_a_parameter_raises(self, graph: GraphFixture) -> None:
        """Scope cannot be widened from a call site.

        Overwriting the caller's value would also be safe, but silently correcting an
        attempt to change tenant is the wrong shape: it turns an escalation attempt into
        a successful query against the right tenant, and nothing is recorded.
        """
        async with read_session(graph.org_a, settings=graph.settings) as session:
            with pytest.raises(UnscopedQuery, match="may not be supplied"):
                await session.run(
                    "MATCH (t:Topic) WHERE t.org_id = $org_id RETURN t",
                    org_id=str(graph.org_b),
                )

    async def test_the_session_binds_its_own_org_id(self, graph: GraphFixture) -> None:
        """The bound value comes from the session, not from anything the caller passed."""
        await seed_topic(graph.settings, graph.org_a, "alpha-only")

        async with read_session(graph.org_a, settings=graph.settings) as session:
            rows = await session.run("RETURN $org_id AS bound")
        assert rows[0]["bound"] == str(graph.org_a)


class TestReadOnlyByDefault:
    @pytest.mark.parametrize(
        "cypher",
        [
            "CREATE (t:Topic {org_id: $org_id})",
            "MATCH (t:Topic) WHERE t.org_id = $org_id SET t.name = 'x'",
            "MATCH (t:Topic) WHERE t.org_id = $org_id DELETE t",
            "MATCH (t:Topic) WHERE t.org_id = $org_id DETACH DELETE t",
            "MATCH (t:Topic) WHERE t.org_id = $org_id REMOVE t.name",
            "MERGE (t:Topic {org_id: $org_id})",
        ],
    )
    async def test_write_clauses_are_refused_in_a_read_session(
        self, graph: GraphFixture, cypher: str
    ) -> None:
        """The check that works on a single instance.

        `default_access_mode=READ_ACCESS` routes to a follower on a cluster, which rejects
        writes — but on the single-instance Community container used in development it is
        only a routing hint and blocks nothing. This static check is what actually holds
        there, which is why both exist.
        """
        async with read_session(graph.org_a, settings=graph.settings) as session:
            with pytest.raises(WriteInReadSession):
                await session.run(cypher)

    async def test_a_property_named_like_a_clause_is_not_a_false_positive(
        self, graph: GraphFixture
    ) -> None:
        """`created_at` contains "create"; the check is word-bounded so it does not fire."""
        async with read_session(graph.org_a, settings=graph.settings) as session:
            rows = await session.run(
                "MATCH (t:Topic) WHERE t.org_id = $org_id "
                "RETURN t.created_at AS created_at, t.settings AS settings"
            )
        assert rows == []

    async def test_a_write_session_may_write(self, graph: GraphFixture) -> None:
        async with write_session(graph.org_a, settings=graph.settings) as session:
            await session.run("CREATE (t:Topic {org_id: $org_id, name: $name})", name="ok")
        async with read_session(graph.org_a, settings=graph.settings) as session:
            rows = await session.run(
                "MATCH (t:Topic) WHERE t.org_id = $org_id RETURN t.name AS name"
            )
        assert [row["name"] for row in rows] == ["ok"]

    async def test_sessions_report_their_mode(self, graph: GraphFixture) -> None:
        async with read_session(graph.org_a, settings=graph.settings) as session:
            assert session.writable is False
            assert session.org_id == graph.org_a
        async with write_session(graph.org_a, settings=graph.settings) as session:
            assert session.writable is True


class TestTransactionSemantics:
    async def test_an_exception_rolls_the_transaction_back(self, graph: GraphFixture) -> None:
        """A half-applied write is worse than a failed one."""
        with pytest.raises(RuntimeError, match="deliberate"):
            async with write_session(graph.org_a, settings=graph.settings) as session:
                await session.run(
                    "CREATE (t:Topic {org_id: $org_id, name: $name})", name="rolled-back"
                )
                raise RuntimeError("deliberate")

        async with read_session(graph.org_a, settings=graph.settings) as session:
            rows = await session.run(
                "MATCH (t:Topic) WHERE t.org_id = $org_id RETURN t.name AS name"
            )
        assert rows == []


class TestSecretHandling:
    def test_settings_never_render_the_password(self, graph_settings: GraphSettings) -> None:
        """§4.9. A repr surfaces in tracebacks, logged exceptions and debug f-strings."""
        rendered = repr(graph_settings)
        assert "<redacted>" in rendered
        assert graph_settings.password not in rendered
        assert graph_settings.password not in str(graph_settings)
        assert graph_settings.password not in f"{graph_settings}"

    def test_the_missing_credential_error_names_variables_not_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
        monkeypatch.setenv("NEO4J_USER", "neo4j")

        with pytest.raises(Exception) as caught:
            get_graph_settings()
        message = str(caught.value)
        assert "NEO4J_PASSWORD" in message
        assert "Secret Manager" in message

    async def test_the_password_never_reaches_a_log_record(
        self, graph: GraphFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Exercised through a real connection, not asserted about in the abstract."""
        with caplog.at_level(logging.DEBUG):
            async with read_session(graph.org_a, settings=graph.settings) as session:
                await session.run("MATCH (t:Topic) WHERE t.org_id = $org_id RETURN t")

        password = graph.settings.password
        for record in caplog.records:
            assert password not in record.getMessage()
