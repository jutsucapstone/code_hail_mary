"""The label and relationship-type allowlist (spec §7, §22).

**Cypher cannot parameterise a label or a relationship type.** `MATCH (n:$label)` is a
syntax error, and there is no binding form for it — so any query whose label varies has
to build that fragment by string interpolation. That is the one place in the graph layer
where a value reaches the query text instead of the parameter map, which makes it the one
place a Cypher injection can start.

The answer is that a label may only ever come from this module. `node_label` and
`relationship_type` take a string and return a member of a closed enum, or raise. There
is no path that accepts arbitrary text, so a caller cannot interpolate something this
module has not vetted. `§22` requires exactly this for the LLM-generated Cypher of Phase
3; it is built here, before there is a generator, because retrofitting a whitelist around
a query builder that already interpolates freely is a rewrite.

Every member is also checked against `_IDENTIFIER` at import time. That is belt and
braces against the enum itself: an allowlist whose entries are unsafe is not an
allowlist, and this catches a member added with a space or a backtick in it at the moment
the module loads rather than the moment it is first used.

The internal ledger label is deliberately **not** here. `migrations.py` owns it, and
keeping it out of the public allowlist means the application-facing API cannot be used to
write to the migration ledger.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

__all__ = [
    "NodeLabel",
    "RelationshipType",
    "UnknownLabel",
    "identifier",
    "node_label",
    "relationship_type",
]


class UnknownLabel(ValueError):
    """A label or relationship type was not in the allowlist.

    A `ValueError`, because that is what it is — but named, so a caller can distinguish
    "you asked for a label that does not exist" from any other bad argument, and so a
    grep for the class name finds every place the allowlist is enforced.
    """


#: What may be interpolated into Cypher as an identifier. Deliberately narrower than
#: Neo4j allows: Neo4j accepts almost anything inside backticks, and "escape it with
#: backticks" is how injection filters are defeated, because a backtick in the value
#: closes the quoting.
_IDENTIFIER: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class NodeLabel(StrEnum):
    """Every node label in the graph (§7). Nothing else may be written."""

    PERSON = "Person"
    PROJECT = "Project"
    DECISION = "Decision"
    MEETING = "Meeting"
    DOCUMENT = "Document"
    MESSAGE = "Message"
    TOPIC = "Topic"
    ACTION_ITEM = "ActionItem"
    TICKET = "Ticket"
    CLIENT = "Client"
    TEAM = "Team"


class RelationshipType(StrEnum):
    """Every relationship type in the graph (§7).

    `ALIAS_OF` is the reversible merge. There is deliberately no destructive merge
    anywhere in this package: a wrong irreversible merge silently corrupts every
    downstream score and cannot be undone.
    """

    WORKS_ON = "WORKS_ON"
    AUTHORED = "AUTHORED"
    ATTENDED = "ATTENDED"
    DECIDED = "DECIDED"
    KNOWS = "KNOWS"
    REPORTS_TO = "REPORTS_TO"
    ALIAS_OF = "ALIAS_OF"
    AFFECTS = "AFFECTS"
    SUPERSEDES = "SUPERSEDES"
    EVIDENCED_BY = "EVIDENCED_BY"
    PRODUCED = "PRODUCED"
    OWNED_BY = "OWNED_BY"
    USES = "USES"
    FOR_CLIENT = "FOR_CLIENT"
    MENTIONS = "MENTIONS"


def identifier(value: str) -> str:
    """Return `value` if it is safe to interpolate into Cypher, else raise.

    Used for the few fragments that are neither a label nor a value — a variable alias in
    a generated predicate, for instance. Not a general escape hatch: it accepts only a
    plain identifier, so it cannot be used to smuggle a clause.
    """
    if not _IDENTIFIER.fullmatch(value):
        raise UnknownLabel(f"{value!r} is not a plain identifier and will not be interpolated")
    return value


def node_label(value: str) -> NodeLabel:
    """Validate a node label against the allowlist."""
    try:
        return NodeLabel(value)
    except ValueError as error:
        raise UnknownLabel(f"{value!r} is not a JUTSU node label") from error


def relationship_type(value: str) -> RelationshipType:
    """Validate a relationship type against the allowlist."""
    try:
        return RelationshipType(value)
    except ValueError as error:
        raise UnknownLabel(f"{value!r} is not a JUTSU relationship type") from error


# Checked at import, not at first use. An allowlist containing an unsafe entry is not an
# allowlist, and the failure should arrive when the module loads in CI rather than when a
# query is first built with it in production.
for _member in (*NodeLabel, *RelationshipType):
    if not _IDENTIFIER.fullmatch(_member.value):
        raise UnknownLabel(f"allowlist entry {_member.value!r} is not a plain identifier")
