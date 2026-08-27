"""Command line entry point for the graph migrations.

A module of its own, and not `if __name__ == "__main__"` inside `migrations.py`.
`python -m jutsu_graph.migrations` imports the `jutsu_graph` package first, whose
`__init__` imports `migrations`, and then runpy executes that same module a second time
as `__main__` — Python warns that the two copies "may result in unpredictable behaviour",
and it is right. Nothing imports this module, so running it has one copy of everything.

    uv run --env-file .env --package jutsu-graph python -m jutsu_graph.cli upgrade
    uv run --env-file .env --package jutsu-graph python -m jutsu_graph.cli current
    uv run --env-file .env --package jutsu-graph python -m jutsu_graph.cli downgrade [version]
"""

from __future__ import annotations

import asyncio
import logging
import sys

from jutsu_graph.driver import close_driver
from jutsu_graph.migrations import applied_versions, downgrade, upgrade

__all__ = ["main"]


async def _run(argv: list[str]) -> int:
    """Exits non-zero on any failure.

    `make migrate` runs this after Alembic and make aborts on a non-zero status, so a
    migration step that failed quietly would report a successful deploy against a schema
    that never changed.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    command = argv[1] if len(argv) > 1 else "upgrade"
    target = argv[2] if len(argv) > 2 else None

    try:
        if command == "upgrade":
            applied = await upgrade(target=target)
            print(f"applied: {', '.join(applied) if applied else 'nothing (already current)'}")
        elif command == "downgrade":
            reverted = await downgrade(target=target)
            print(f"reverted: {', '.join(reverted) if reverted else 'nothing'}")
        elif command == "current":
            versions = await applied_versions()
            print(f"applied: {', '.join(versions) if versions else 'none'}")
        else:
            print(f"unknown command {command!r}; expected upgrade, downgrade or current")
            return 2
    finally:
        await close_driver()

    return 0


def main() -> int:
    return asyncio.run(_run(sys.argv))


if __name__ == "__main__":
    sys.exit(main())
