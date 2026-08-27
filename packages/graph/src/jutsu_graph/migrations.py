"""Numbered Cypher migrations and the ledger that records them (§4.12).

Alembic owns the Postgres schema; nothing equivalent ships for Neo4j, so this is the
smallest runner that satisfies the same rule — **every schema change is a migration, and
migrations are reviewable, repeatable and reversible.**

The shape mirrors `packages/db` deliberately, because the failure modes are the same:

  * Files are numbered and paired: `001_constraints.up.cypher` and its `.down.cypher`.
    A migration with no down file is refused at load time, not at rollback time, which is
    the moment you least want to discover it.
  * A ledger node records what has been applied. `upgrade` skips anything already there,
    so running it twice applies nothing.
  * Every applied migration's **checksum** is stored and re-verified. Editing a migration
    that has already run is how two environments silently diverge; here it raises.

**Migrations are not org-scoped, and cannot be.** Constraints and indexes are
database-wide objects — there is no such thing as a constraint belonging to one tenant —
so this module uses `ddl_session`, the deliberately conspicuous unscoped path, rather
than `write_session`, whose `$org_id` requirement it could not satisfy. That is the same
split `packages/db` makes between the migration role and the application role, for the
same reason.

Run it with `make migrate`, or directly:

    uv run --env-file .env --package jutsu-graph python -m jutsu_graph.cli upgrade
    uv run --env-file .env --package jutsu-graph python -m jutsu_graph.cli current
    uv run --env-file .env --package jutsu-graph python -m jutsu_graph.cli downgrade
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from jutsu_graph.driver import DdlSession, GraphSettings, ddl_session

__all__ = [
    "LEDGER_CONSTRAINT",
    "LEDGER_LABEL",
    "MIGRATIONS_DIR",
    "ChecksumMismatch",
    "Migration",
    "MissingDownMigration",
    "applied_versions",
    "downgrade",
    "load_migrations",
    "split_statements",
    "upgrade",
]

logger = logging.getLogger("jutsu.graph.migrations")

MIGRATIONS_DIR: Final = Path(__file__).parent / "migrations"

#: The ledger. Underscore-prefixed and deliberately absent from `labels.py`: the
#: application-facing allowlist is what a query builder may write to, and the record of
#: which migrations have run is not something the application should be able to edit.
LEDGER_LABEL: Final = "_JutsuGraphMigration"

#: Name of the constraint backing the ledger. Checked before creation rather than
#: relying on IF NOT EXISTS alone, so a successful run prints nothing.
LEDGER_CONSTRAINT: Final = "jutsu_graph_migration_version"

#: `001_constraints` -> version `001`, name `constraints`.
_FILENAME: Final = re.compile(
    r"^(?P<version>\d{3})_(?P<name>[a-z0-9_]+)\.(?P<direction>up|down)\.cypher$"
)

#: Line comments. Stripped before splitting on `;`, so a semicolon inside a comment
#: cannot invent an empty statement.
_LINE_COMMENT: Final = re.compile(r"//[^\n]*")


class ChecksumMismatch(RuntimeError):
    """An already-applied migration's file has changed since it ran.

    Editing an applied migration is the quiet way two environments end up with different
    schemas while both report the same version. The fix is a new migration, never an edit
    to an old one.
    """


class MissingDownMigration(RuntimeError):
    """A migration has no `.down.cypher`. Refused at load time (§4.12 — reversible)."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    name: str
    up: str
    down: str

    @property
    def checksum(self) -> str:
        """Hash of the up statements. What the ledger stores and re-verifies."""
        return hashlib.sha256(self.up.encode("utf-8")).hexdigest()


def split_statements(source: str) -> list[str]:
    """Cypher text into individual statements.

    Line comments are stripped first, then the text is split on `;`. Neo4j executes one
    statement per call, so a file has to be split somewhere, and a full Cypher parser is
    not warranted for DDL that this repository writes and reviews.

    The limitation, stated rather than discovered: a semicolon inside a string literal
    would split wrongly. No schema statement needs one, and the loader below is only ever
    pointed at files in this package.
    """
    without_comments = _LINE_COMMENT.sub("", source)
    return [statement.strip() for statement in without_comments.split(";") if statement.strip()]


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Every migration on disk, ordered by version.

    A version with an up file and no down file raises here — at load, so `upgrade`
    refuses to apply something it could not roll back, rather than finding out during an
    incident.
    """
    ups: dict[str, tuple[str, str]] = {}
    downs: dict[str, str] = {}

    for path in sorted(directory.glob("*.cypher")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise ValueError(
                f"{path.name} does not match NNN_name.(up|down).cypher, so its order and "
                "direction are ambiguous."
            )
        version = match["version"]
        content = path.read_text(encoding="utf-8")
        if match["direction"] == "up":
            ups[version] = (match["name"], content)
        else:
            downs[version] = content

    migrations: list[Migration] = []
    for version in sorted(ups):
        name, up = ups[version]
        if version not in downs:
            raise MissingDownMigration(
                f"Migration {version}_{name} has no {version}_{name}.down.cypher. Every "
                "schema change must be reversible (§4.12)."
            )
        migrations.append(Migration(version=version, name=name, up=up, down=downs[version]))
    return migrations


async def _ensure_ledger(session: DdlSession) -> None:
    """Create the ledger's uniqueness constraint.

    Chicken and egg, resolved by doing it first and idempotently: the ledger is what
    records applied migrations, so it cannot itself be one.
    """
    existing = await session.run("SHOW CONSTRAINTS YIELD name RETURN name")
    if any(row["name"] == LEDGER_CONSTRAINT for row in existing):
        return

    await session.run(
        f"CREATE CONSTRAINT {LEDGER_CONSTRAINT} IF NOT EXISTS "
        f"FOR (m:{LEDGER_LABEL}) REQUIRE m.version IS UNIQUE"
    )


async def _applied(session: DdlSession) -> dict[str, str]:
    """Version to checksum, for everything the ledger says has run."""
    rows = await session.run(
        f"MATCH (m:{LEDGER_LABEL}) RETURN m.version AS version, m.checksum AS checksum "
        f"ORDER BY m.version"
    )
    return {row["version"]: row["checksum"] for row in rows}


async def applied_versions(*, settings: GraphSettings | None = None) -> list[str]:
    """Versions currently recorded as applied, oldest first."""
    async with ddl_session(settings=settings) as session:
        await _ensure_ledger(session)
        return list((await _applied(session)).keys())


async def upgrade(*, target: str | None = None, settings: GraphSettings | None = None) -> list[str]:
    """Apply every migration not yet in the ledger. Returns what it applied.

    `target` is the last version to apply; `None` applies everything.

    Idempotent by construction twice over: the ledger stops an applied migration being
    re-run, and every statement in `001` is `IF NOT EXISTS` so re-running one would be a
    no-op anyway. Belt and braces, because the cost of a non-idempotent schema step is a
    failed deploy at the worst moment.
    """
    migrations = load_migrations()
    applied: list[str] = []

    async with ddl_session(settings=settings) as session:
        await _ensure_ledger(session)
        ledger = await _applied(session)

        for migration in migrations:
            if target is not None and migration.version > target:
                break

            recorded = ledger.get(migration.version)
            if recorded is not None:
                if recorded != migration.checksum:
                    raise ChecksumMismatch(
                        f"Migration {migration.version}_{migration.name} has changed since "
                        f"it was applied. Add a new migration instead of editing an "
                        f"applied one."
                    )
                continue

            for statement in split_statements(migration.up):
                await session.run(statement)

            await session.run(
                f"MERGE (m:{LEDGER_LABEL} {{version: $version}}) "
                f"SET m.name = $name, m.checksum = $checksum, m.applied_at = $applied_at",
                version=migration.version,
                name=migration.name,
                checksum=migration.checksum,
                applied_at=datetime.now(UTC).isoformat(),
            )
            # The version and the name, and nothing else. A migration's *content* can
            # name a label or a property; the fact that it ran cannot.
            logger.info("graph_migration_applied version=%s", migration.version)
            applied.append(migration.version)

    return applied


async def downgrade(
    *, target: str | None = None, settings: GraphSettings | None = None
) -> list[str]:
    """Revert applied migrations, newest first. Returns what it reverted.

    `target` is the version to stop *at* — everything after it is reverted and it is
    kept, matching Alembic. `None` reverts everything, which is `make migrate-down`'s
    `downgrade base`.
    """
    migrations = {migration.version: migration for migration in load_migrations()}
    reverted: list[str] = []

    async with ddl_session(settings=settings) as session:
        await _ensure_ledger(session)
        ledger = await _applied(session)

        for version in sorted(ledger, reverse=True):
            if target is not None and version <= target:
                break
            migration = migrations.get(version)
            if migration is None:
                raise MissingDownMigration(
                    f"The ledger records {version} as applied but no migration file for "
                    f"it exists, so it cannot be reverted."
                )

            for statement in split_statements(migration.down):
                await session.run(statement)

            await session.run(
                f"MATCH (m:{LEDGER_LABEL} {{version: $version}}) DELETE m", version=version
            )
            logger.info("graph_migration_reverted version=%s", version)
            reverted.append(version)

    return reverted
