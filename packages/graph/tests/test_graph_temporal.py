"""Bitemporal correctness (spec §7).

The property under test throughout: **superseding closes an interval and never removes a
relationship.** A store that deleted the old version would answer "who owned this in
March" with today's answer and no indication it had done so, which is the failure that
makes a decision ledger worthless.

Written against a real Neo4j because the comparisons are the thing being tested — the
driver's datetime round trip, `IS NULL` on an absent property, and half-open interval
semantics against server-side temporal types.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from graph_support import (  # noqa: F401 — imported so pytest registers the fixtures
    GraphFixture,
    graph_fixture,
)
from jutsu_graph.driver import GraphSettings, WriteInReadSession, read_session, write_session
from jutsu_graph.labels import RelationshipType, UnknownLabel
from jutsu_graph.temporal import (
    NaiveTimestamp,
    UntemporalQuery,
    as_of,
    current_filter,
    supersede,
    temporal_filter,
    temporal_properties,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 6, 1, tzinfo=UTC)
T2 = datetime(2026, 9, 1, tzinfo=UTC)

WORKS_ON = RelationshipType.WORKS_ON.value


async def make_edge(
    settings: GraphSettings,
    org_id: uuid.UUID,
    *,
    rel_id: str,
    role: str,
    valid_from: datetime,
) -> None:
    """One Person, one Project and a WORKS_ON between them, temporally stamped.

    Node identities are unique per call because `person_org_email` and `project_key` are
    uniqueness constraints — a helper that reused them would fail on its second call for
    a reason unrelated to what the test is checking.
    """
    properties = temporal_properties(valid_from=valid_from, recorded_at=valid_from)
    async with write_session(org_id, settings=settings) as session:
        await session.run(
            f"CREATE (a:Person {{org_id: $org_id, id: $a_id, email: $email}}) "
            f"CREATE (b:Project {{org_id: $org_id, id: $b_id, key: $key}}) "
            f"CREATE (a)-[r:{WORKS_ON} {{id: $rel_id, org_id: $org_id, role: $role, "
            f"valid_from: $valid_from, valid_to: $valid_to, recorded_at: $recorded_at}}]->(b)",
            a_id=str(uuid.uuid4()),
            b_id=str(uuid.uuid4()),
            email=f"{uuid.uuid4()}@example.com",
            key=str(uuid.uuid4()),
            rel_id=rel_id,
            role=role,
            **properties,
        )


async def roles_at(settings: GraphSettings, org_id: uuid.UUID, at: datetime) -> list[str]:
    query = (
        f"MATCH ()-[r:{WORKS_ON}]->() "
        f"WHERE r.org_id = $org_id AND {temporal_filter('r')} "
        f"RETURN r.role AS role ORDER BY role"
    )
    async with read_session(org_id, settings=settings) as session:
        rows = await as_of(session, query, at=at)
    return [row["role"] for row in rows]


async def relationship_count(settings: GraphSettings, org_id: uuid.UUID) -> int:
    async with read_session(org_id, settings=settings) as session:
        rows = await session.run(
            f"MATCH ()-[r:{WORKS_ON}]->() WHERE r.org_id = $org_id RETURN count(r) AS total"
        )
    return int(rows[0]["total"])


# --------------------------------------------------------------------------- predicates


class TestPredicates:
    def test_the_temporal_filter_is_half_open(self) -> None:
        """Closed on both ends, a point-in-time query returns two versions of one fact."""
        predicate = temporal_filter("r")
        assert "r.valid_from <= $as_of" in predicate
        assert "r.valid_to IS NULL OR r.valid_to > $as_of" in predicate

    def test_the_current_filter_uses_null_not_a_comparison(self) -> None:
        assert current_filter("r") == "(r.valid_to IS NULL)"

    def test_an_alias_that_is_not_an_identifier_is_refused(self) -> None:
        """The one fragment this module interpolates, and it goes through the allowlist."""
        with pytest.raises(UnknownLabel):
            temporal_filter("r) DELETE (x")
        with pytest.raises(UnknownLabel):
            current_filter("r.valid_to IS NULL OR true")

    def test_temporal_properties_opens_an_interval(self) -> None:
        properties = temporal_properties(valid_from=T0, recorded_at=T1)
        assert properties == {"valid_from": T0, "valid_to": None, "recorded_at": T1}

    def test_valid_time_and_transaction_time_are_separate(self) -> None:
        """Two clocks. Conflating them is the mistake the module exists to prevent."""
        properties = temporal_properties(valid_from=T0, recorded_at=T2)
        assert properties["valid_from"] != properties["recorded_at"]


class TestNaiveTimestampsAreRefused:
    def test_temporal_properties_refuses_a_naive_valid_from(self) -> None:
        with pytest.raises(NaiveTimestamp, match="valid_from"):
            temporal_properties(valid_from=datetime(2026, 1, 1), recorded_at=T0)  # noqa: DTZ001

    def test_temporal_properties_refuses_a_naive_recorded_at(self) -> None:
        with pytest.raises(NaiveTimestamp, match="recorded_at"):
            temporal_properties(valid_from=T0, recorded_at=datetime(2026, 1, 1))  # noqa: DTZ001

    async def test_supersede_refuses_a_naive_instant(self, graph: GraphFixture) -> None:
        async with write_session(graph.org_a, settings=graph.settings) as session:
            with pytest.raises(NaiveTimestamp):
                await supersede(session, rel_id="x", at=datetime(2026, 1, 1))  # noqa: DTZ001


# --------------------------------------------------------------------------- supersede


class TestSupersede:
    async def test_it_closes_the_interval_without_deleting(self, graph: GraphFixture) -> None:
        """The single most important assertion in this module."""
        await make_edge(graph.settings, graph.org_a, rel_id="r1", role="engineer", valid_from=T0)
        assert await relationship_count(graph.settings, graph.org_a) == 1

        async with write_session(graph.org_a, settings=graph.settings) as session:
            assert await supersede(session, rel_id="r1", at=T1) == 1

        # Still there. Closed, not removed.
        assert await relationship_count(graph.settings, graph.org_a) == 1

        async with read_session(graph.org_a, settings=graph.settings) as session:
            rows = await session.run(
                f"MATCH ()-[r:{WORKS_ON}]->() WHERE r.org_id = $org_id AND r.id = $rel_id "
                f"RETURN r.valid_to AS valid_to",
                rel_id="r1",
            )
        assert rows[0]["valid_to"].to_native() == T1

    async def test_it_is_idempotent(self, graph: GraphFixture) -> None:
        """A second call closes nothing and does not move the first `valid_to`."""
        await make_edge(graph.settings, graph.org_a, rel_id="r1", role="engineer", valid_from=T0)

        async with write_session(graph.org_a, settings=graph.settings) as session:
            assert await supersede(session, rel_id="r1", at=T1) == 1
            assert await supersede(session, rel_id="r1", at=T2) == 0

        async with read_session(graph.org_a, settings=graph.settings) as session:
            rows = await session.run(
                f"MATCH ()-[r:{WORKS_ON}]->() WHERE r.org_id = $org_id AND r.id = $rel_id "
                f"RETURN r.valid_to AS valid_to",
                rel_id="r1",
            )
        assert rows[0]["valid_to"].to_native() == T1

    async def test_an_unknown_relationship_closes_nothing(self, graph: GraphFixture) -> None:
        async with write_session(graph.org_a, settings=graph.settings) as session:
            assert await supersede(session, rel_id="does-not-exist", at=T1) == 0

    async def test_it_cannot_reach_another_tenant(self, graph: GraphFixture) -> None:
        """Adversarial: org B holds the id and tries to close org A's relationship."""
        await make_edge(
            graph.settings, graph.org_a, rel_id="shared-id", role="engineer", valid_from=T0
        )

        async with write_session(graph.org_b, settings=graph.settings) as session:
            assert await supersede(session, rel_id="shared-id", at=T1) == 0

        async with read_session(graph.org_a, settings=graph.settings) as session:
            rows = await session.run(
                f"MATCH ()-[r:{WORKS_ON}]->() WHERE r.org_id = $org_id AND r.id = $rel_id "
                f"RETURN r.valid_to AS valid_to",
                rel_id="shared-id",
            )
        assert rows[0]["valid_to"] is None, "another tenant closed this interval"

    async def test_it_requires_a_write_session(self, graph: GraphFixture) -> None:
        async with read_session(graph.org_a, settings=graph.settings) as session:
            with pytest.raises(WriteInReadSession):
                await supersede(session, rel_id="r1", at=T1)


# --------------------------------------------------------------------------- as_of


class TestAsOf:
    async def test_it_returns_the_state_at_a_past_instant(self, graph: GraphFixture) -> None:
        """Two versions of one fact, and the question "what did we believe in March"."""
        await make_edge(graph.settings, graph.org_a, rel_id="r1", role="engineer", valid_from=T0)
        async with write_session(graph.org_a, settings=graph.settings) as session:
            await supersede(session, rel_id="r1", at=T1)
        await make_edge(graph.settings, graph.org_a, rel_id="r2", role="lead", valid_from=T1)

        assert await roles_at(graph.settings, graph.org_a, T0 + timedelta(days=1)) == ["engineer"]
        assert await roles_at(graph.settings, graph.org_a, T1 + timedelta(days=1)) == ["lead"]

    async def test_the_interval_is_half_open_at_the_boundary(self, graph: GraphFixture) -> None:
        """Exactly at the supersede instant, the new version holds and the old does not.

        A closed interval would return both here, and every consumer would need a rule for
        breaking the tie.
        """
        await make_edge(graph.settings, graph.org_a, rel_id="r1", role="engineer", valid_from=T0)
        async with write_session(graph.org_a, settings=graph.settings) as session:
            await supersede(session, rel_id="r1", at=T1)
        await make_edge(graph.settings, graph.org_a, rel_id="r2", role="lead", valid_from=T1)

        assert await roles_at(graph.settings, graph.org_a, T1) == ["lead"]

    async def test_nothing_is_valid_before_it_began(self, graph: GraphFixture) -> None:
        await make_edge(graph.settings, graph.org_a, rel_id="r1", role="engineer", valid_from=T1)
        assert await roles_at(graph.settings, graph.org_a, T0) == []

    async def test_the_current_filter_sees_only_open_intervals(self, graph: GraphFixture) -> None:
        await make_edge(graph.settings, graph.org_a, rel_id="r1", role="engineer", valid_from=T0)
        async with write_session(graph.org_a, settings=graph.settings) as session:
            await supersede(session, rel_id="r1", at=T1)
        await make_edge(graph.settings, graph.org_a, rel_id="r2", role="lead", valid_from=T1)

        async with read_session(graph.org_a, settings=graph.settings) as session:
            rows = await session.run(
                f"MATCH ()-[r:{WORKS_ON}]->() "
                f"WHERE r.org_id = $org_id AND {current_filter('r')} "
                f"RETURN r.role AS role"
            )
        assert [row["role"] for row in rows] == ["lead"]

    async def test_a_query_without_the_parameter_is_refused(self, graph: GraphFixture) -> None:
        """Same shape as `UnscopedQuery`: omitting the filter returns every version."""
        async with read_session(graph.org_a, settings=graph.settings) as session:
            with pytest.raises(UntemporalQuery, match="does not reference"):
                await as_of(
                    session,
                    f"MATCH ()-[r:{WORKS_ON}]->() WHERE r.org_id = $org_id RETURN r",
                    at=T1,
                )

    async def test_it_refuses_a_naive_instant(self, graph: GraphFixture) -> None:
        async with read_session(graph.org_a, settings=graph.settings) as session:
            with pytest.raises(NaiveTimestamp):
                await as_of(
                    session,
                    f"MATCH ()-[r:{WORKS_ON}]->() WHERE r.org_id = $org_id "
                    f"AND {temporal_filter('r')} RETURN r",
                    at=datetime(2026, 1, 1),  # noqa: DTZ001
                )

    async def test_it_cannot_read_another_tenants_history(self, graph: GraphFixture) -> None:
        await make_edge(graph.settings, graph.org_a, rel_id="r1", role="engineer", valid_from=T0)
        assert await roles_at(graph.settings, graph.org_b, T0 + timedelta(days=1)) == []
