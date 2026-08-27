"""Bitemporal helpers (spec §7).

**Bitemporality is mandatory and starts at migration 001.** Two clocks, and conflating
them is the mistake this module exists to prevent:

    valid_from / valid_to    when the fact was true *in the world*
    recorded_at              when JUTSU *learned* it

A decision ledger that cannot answer "what did we believe on the 3rd of March" is not a
ledger, it is a current-state table with extra columns. The spec is explicit that
retrofitting this in week 8 means rewriting every edge and every template, which is why
these functions land now, before there is anything to store.

**Superseding never deletes.** `supersede` closes a relationship's validity interval by
setting `valid_to`; the relationship stays in the graph forever. Deleting it would make
every historical query silently wrong and could not be undone — the same reason §7
requires `ALIAS_OF` for merges rather than a destructive one.

What this module deliberately does **not** provide is a way to write relationships. The
shape of an extracted edge — its `evidence[]`, its `confidence`, its `extractor_version` —
is §10's contract, and inventing it here would mean guessing at it and then changing it.
`temporal_properties` gives the temporal half, which is unambiguous today, and the
extraction slice supplies the rest.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from neo4j import Record

from jutsu_graph.driver import GraphSession
from jutsu_graph.labels import identifier

__all__ = [
    "AS_OF_PARAMETER",
    "RECORDED_AT",
    "VALID_FROM",
    "VALID_TO",
    "NaiveTimestamp",
    "UntemporalQuery",
    "as_of",
    "current_filter",
    "supersede",
    "temporal_filter",
    "temporal_properties",
]

#: Property names, defined once. Every template and every test reads them from here, so a
#: rename is a change in one place rather than a grep across the codebase.
VALID_FROM: Final = "valid_from"
VALID_TO: Final = "valid_to"
RECORDED_AT: Final = "recorded_at"

#: The parameter `as_of` binds. Mirrors `ORG_PARAMETER` in `driver.py`: named once so the
#: check and the binding cannot drift.
AS_OF_PARAMETER: Final = "as_of"


class NaiveTimestamp(ValueError):
    """A timestamp arrived without a timezone.

    Refused rather than assumed to be UTC. A bitemporal store whose instants are
    ambiguous by an unknown offset is worse than one with no history at all, because the
    answers still look plausible. The corpus spans custodians in several timezones and
    the product answers questions about ordering.
    """


class UntemporalQuery(RuntimeError):
    """An `as_of` query carried no temporal predicate.

    The same failure shape as `UnscopedQuery`: a query that reads correctly, omits the
    filter, and silently returns the union of every version of every fact rather than the
    state at one instant.
    """


def _require_aware(value: datetime, argument: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveTimestamp(
            f"{argument} has no timezone. Pass an aware datetime — "
            "`datetime.now(UTC)`, not `datetime.now()`."
        )
    return value


def temporal_filter(alias: str = "r") -> str:
    """The predicate selecting the version of a relationship valid at `$as_of`.

    Half-open on purpose: `valid_from <= $as_of < valid_to`. A closed interval would make
    a fact valid at the instant it was superseded *and* at the instant its replacement
    began, so a point-in-time query would return two versions of one fact and every
    consumer would have to break the tie.

    `alias` goes through `identifier`, so it cannot carry a clause. It is the only text
    this module interpolates.
    """
    name = identifier(alias)
    return (
        f"({name}.{VALID_FROM} <= ${AS_OF_PARAMETER} AND "
        f"({name}.{VALID_TO} IS NULL OR {name}.{VALID_TO} > ${AS_OF_PARAMETER}))"
    )


def current_filter(alias: str = "r") -> str:
    """The predicate selecting the version valid *now*.

    `valid_to IS NULL` and not `valid_to > datetime()`. Null means open-ended, which is a
    different statement from "ends at some point in the future" — and only the null form
    can use an index scan without evaluating a function per row.
    """
    name = identifier(alias)
    return f"({name}.{VALID_TO} IS NULL)"


def temporal_properties(*, valid_from: datetime, recorded_at: datetime) -> dict[str, Any]:
    """The temporal half of a relationship's properties.

    `valid_to` is present and `None`, deliberately. Neo4j does not distinguish an absent
    property from a null one on read, but writing it makes the open interval explicit to
    anyone reading the write site, and it means the property exists in the store's schema
    the first time a relationship is created rather than the first time one is superseded.

    Both instants must be timezone-aware; see `NaiveTimestamp`.
    """
    return {
        VALID_FROM: _require_aware(valid_from, "valid_from"),
        VALID_TO: None,
        RECORDED_AT: _require_aware(recorded_at, "recorded_at"),
    }


async def supersede(session: GraphSession, *, rel_id: str, at: datetime) -> int:
    """Close a relationship's validity interval. Returns how many were closed.

    Matches on the application-assigned `id` property, never on Neo4j's internal element
    id: that one is not stable across a restore or a store copy, and a bitemporal graph is
    exactly the kind of thing that gets restored.

    Only closes intervals that are still open (`valid_to IS NULL`), so calling it twice
    is safe and the second call returns 0 rather than moving a `valid_to` that was
    already set. Superseding a fact does not un-supersede and re-supersede it.

    **Never deletes.** The relationship remains, and `as_of` at any instant before `at`
    still finds it. Requires a write session — a read session refuses the `SET`.
    """
    _require_aware(at, "at")
    rows = await session.run(
        "MATCH ()-[r]->() "
        "WHERE r.id = $rel_id AND r.org_id = $org_id AND r.valid_to IS NULL "
        "SET r.valid_to = $at "
        "RETURN count(r) AS closed",
        rel_id=rel_id,
        at=at,
    )
    return int(rows[0]["closed"]) if rows else 0


async def as_of(
    session: GraphSession, cypher: str, /, *, at: datetime, **parameters: Any
) -> list[Record]:
    """Run a query at a point in time, with `$as_of` bound.

    The query must reference `$as_of` itself — this does not rewrite Cypher. Rewriting a
    caller's query to inject a temporal predicate would mean parsing Cypher, and a parser
    that is wrong about one clause silently returns the wrong history. So the contract is
    the same one `driver.py` uses for `$org_id`: build the predicate with
    `temporal_filter()`, and this refuses the query if you did not.

    `cypher` is positional-only for the same reason it is in `GraphSession.run`.
    """
    _require_aware(at, "at")
    if f"${AS_OF_PARAMETER}" not in cypher:
        raise UntemporalQuery(
            "This query does not reference $as_of, so it would return every version of "
            "every matching fact rather than the state at one instant. Build the "
            "predicate with `temporal_filter(alias)` and interpolate it into your query."
        )
    return await session.run(cypher, **{AS_OF_PARAMETER: at}, **parameters)
