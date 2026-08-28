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
from jutsu_connectors.enron import ManifestConnector, SampleManifest, UnparsableManifest
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

    # A source that names a manifest ingests the sample, not the directory (§19).
    #
    # This is the one seam where "sample complete threads, never random messages" reaches
    # the ingestion path. Without it `make seed` walks the corpus and takes whatever it
    # meets up to `--max-documents`, which on the real Enron tree is exactly the random
    # sampling §19 forbids — and the damage is invisible until entity resolution has
    # nothing to resolve, weeks later.
    manifest_path = config.get("manifest")
    if manifest_path is not None:
        if not isinstance(manifest_path, str) or not manifest_path:
            raise UnsupportedSource("local source config has an unusable manifest path")
        try:
            manifest = SampleManifest.from_json(Path(manifest_path).read_text(encoding="utf-8"))
        except OSError as error:
            raise UnsupportedSource("the sample manifest could not be read") from error
        except UnparsableManifest as error:
            # Permanent by construction: re-reading the same file gives the same answer,
            # and the fix is to re-run the sampler rather than to retry the job.
            raise UnsupportedSource(f"the sample manifest is unusable: {error}") from error
        return ManifestConnector(Path(root), manifest)

    return LocalConnector(Path(root))
