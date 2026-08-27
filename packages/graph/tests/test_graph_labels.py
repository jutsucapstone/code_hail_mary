"""The label allowlist (§7, §22).

No database. This is the one part of the graph layer that is pure, and it is deliberately
the part that decides what may be interpolated into Cypher — so these tests run on every
machine, with or without Docker, and never skip.
"""

from __future__ import annotations

import pytest
from jutsu_graph.labels import (
    NodeLabel,
    RelationshipType,
    UnknownLabel,
    identifier,
    node_label,
    relationship_type,
)

#: §7's node table and edge list, transcribed independently of the enum. If the two
#: disagree, one of them drifted from the spec and this test says which.
SPEC_NODES = {
    "Person",
    "Project",
    "Decision",
    "Meeting",
    "Document",
    "Message",
    "Topic",
    "ActionItem",
    "Ticket",
    "Client",
    "Team",
}

SPEC_RELATIONSHIPS = {
    "WORKS_ON",
    "AUTHORED",
    "ATTENDED",
    "DECIDED",
    "KNOWS",
    "REPORTS_TO",
    "ALIAS_OF",
    "AFFECTS",
    "SUPERSEDES",
    "EVIDENCED_BY",
    "PRODUCED",
    "OWNED_BY",
    "USES",
    "FOR_CLIENT",
    "MENTIONS",
}

#: Cypher fragments that would change a query's meaning if one reached the text.
INJECTIONS = [
    "Person) DETACH DELETE (n",
    "Person`) RETURN 1 //",
    "Person WHERE 1=1",
    "Person;MATCH (x) DELETE x",
    "Person\nMATCH (n) RETURN n",
    "",
    " ",
    "1Person",
    "Person-Project",
]


class TestCatalogueMatchesTheSpec:
    def test_every_spec_node_label_is_present(self) -> None:
        assert {label.value for label in NodeLabel} == SPEC_NODES

    def test_every_spec_relationship_is_present(self) -> None:
        assert {rel.value for rel in RelationshipType} == SPEC_RELATIONSHIPS

    def test_the_reversible_merge_edge_exists(self) -> None:
        """§7 requires `ALIAS_OF`. There is no destructive merge anywhere in the package."""
        assert RelationshipType.ALIAS_OF.value == "ALIAS_OF"


class TestValidation:
    @pytest.mark.parametrize("value", sorted(SPEC_NODES))
    def test_a_known_label_round_trips(self, value: str) -> None:
        assert node_label(value).value == value

    @pytest.mark.parametrize("value", sorted(SPEC_RELATIONSHIPS))
    def test_a_known_relationship_round_trips(self, value: str) -> None:
        assert relationship_type(value).value == value

    @pytest.mark.parametrize("value", INJECTIONS)
    def test_an_injection_is_refused_as_a_label(self, value: str) -> None:
        """Cypher cannot parameterise a label, so this is the only thing standing there."""
        with pytest.raises(UnknownLabel):
            node_label(value)

    @pytest.mark.parametrize("value", INJECTIONS)
    def test_an_injection_is_refused_as_a_relationship(self, value: str) -> None:
        with pytest.raises(UnknownLabel):
            relationship_type(value)

    def test_a_label_that_merely_looks_plausible_is_refused(self) -> None:
        """Case matters. `person` is not `Person`, and near-misses must not pass."""
        for value in ("person", "PERSON", "Persons", "Projekt"):
            with pytest.raises(UnknownLabel):
                node_label(value)


class TestIdentifier:
    @pytest.mark.parametrize("value", ["r", "rel", "_x", "a1", "valid_from"])
    def test_a_plain_identifier_is_returned(self, value: str) -> None:
        assert identifier(value) == value

    @pytest.mark.parametrize(
        "value",
        ["r) DELETE (x", "r.valid_to", "r r", "", "1r", "r`", "r-1", "r;", "r\n"],
    )
    def test_anything_else_is_refused(self, value: str) -> None:
        with pytest.raises(UnknownLabel, match="not a plain identifier"):
            identifier(value)

    def test_backticks_are_refused_rather_than_escaped(self) -> None:
        """Neo4j accepts almost anything inside backticks, which is exactly why quoting
        is not the defence: a backtick in the value closes the quoting."""
        with pytest.raises(UnknownLabel):
            identifier("`r`")


class TestAllowlistIntegrity:
    def test_every_entry_is_safe_to_interpolate(self) -> None:
        """The import-time check, asserted again here so the reason is visible.

        An allowlist containing an unsafe entry is not an allowlist. This is what makes
        the interpolation in `temporal.py` and the migration runner defensible.
        """
        for member in (*NodeLabel, *RelationshipType):
            assert identifier(member.value) == member.value
