"""Building a connector from a `sources` row.

One function, and it is deliberately dull. The interesting property is what it refuses:
an unknown `source_system` raises rather than falling back to something, because a
connector chosen by default is a connector nobody decided on.

**No credentials pass through here, and none are stored.** `LocalConnector` needs a
filesystem path and nothing else, which is exactly why it is the connector S8 ships with:
the ingestion pipeline can be proven end to end without an OAuth flow, a token store or a
secret in the database. When provider connectors land in Phase 4 they bring their own
credential handling — Secret Manager, per §4.10 — and this function will hand them a
reference, never a value. `config_json` is for configuration; a secret in it would be a
secret in a database backup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jutsu_connectors import LocalConnector
from jutsu_core import Connector, SourceSystem

__all__ = ["UnsupportedSource", "connector_for"]


class UnsupportedSource(RuntimeError):
    """A source whose system has no connector in this build.

    Its own class so a job can classify it as permanent: no amount of retrying adds a
    connector, and the job should fail rather than occupy the queue until it dead-letters.
    """


def connector_for(system: SourceSystem, config: dict[str, Any]) -> Connector:
    """The connector for one source row.

    Only `SourceSystem.LOCAL` is implemented. Gmail, Slack, Jira and the rest are Phase 4
    and will implement the same three read-only methods against the same models — which is
    what `local` exists to demonstrate before there is an OAuth flow in the way.
    """
    if system is not SourceSystem.LOCAL:
        raise UnsupportedSource(f"no connector for source system {system.value}")

    root = config.get("root")
    if not isinstance(root, str) or not root:
        raise UnsupportedSource("local source config is missing a corpus root")

    return LocalConnector(Path(root))
