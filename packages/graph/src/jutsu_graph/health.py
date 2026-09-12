"""Is the graph there, and should anything be waiting to find out?

`/readyz` is polled by the platform, and the platform's patience is what decides whether a
revision serves traffic. So the probe here is bounded twice over:

  * **A timeout.** `verify_connectivity` against an unreachable host waits for the
    driver's own connection timeout, which is long enough to make a readiness probe fail
    on a dependency that is documented as optional. Two seconds is longer than a healthy
    round trip to AuraDB from the same region and far shorter than any probe budget.
  * **A cache.** Readiness is polled every few seconds by more than one prober; asking
    Neo4j every time turns a health endpoint into a load generator, and the answer does
    not change that fast.

**A graph that is down is never an outage of JUTSU.** `not_configured` and `degraded` are
both reported and neither fails readiness — that decision lives in `/readyz`, and this
module only supplies the word. Vector retrieval does not depend on this store, and the
hybrid path falls back to it (ADR 0022).
"""

from __future__ import annotations

import asyncio
import time
from enum import StrEnum
from typing import Final

from jutsu_graph.driver import GraphSettings, MissingGraphSettings, get_driver, get_graph_settings

__all__ = ["DEFAULT_PROBE_TIMEOUT_S", "GraphStatus", "probe", "reset_probe_cache"]

#: Longer than a healthy round trip, shorter than any readiness budget.
DEFAULT_PROBE_TIMEOUT_S: Final = 2.0

#: How long an answer is reused. Readiness polls far faster than a graph's reachability
#: changes, and a cache miss costs a connection.
DEFAULT_PROBE_TTL_S: Final = 10.0


class GraphStatus(StrEnum):
    """Three states, and the difference between the first two is the whole point.

    `NOT_CONFIGURED` is a statement about the deployment: no `NEO4J_URI`, nothing to be
    unwell. `DEGRADED` is a statement about the store: it was configured and did not
    answer, which is worth an operator's attention even though nothing user-facing is
    broken.
    """

    NOT_CONFIGURED = "not_configured"
    OK = "ok"
    DEGRADED = "degraded"


_cached: tuple[float, GraphStatus] | None = None


def reset_probe_cache() -> None:
    """Drop the cached answer. For tests, and for a configuration change in development."""
    global _cached
    _cached = None


async def probe(
    *,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    ttl_s: float = DEFAULT_PROBE_TTL_S,
    settings: GraphSettings | None = None,
) -> GraphStatus:
    """Whether the graph is configured and answering.

    Swallows every exception on purpose, exactly as `driver.ping` does: a readiness probe
    reports a state rather than raising one, and the text of a connection failure can
    carry a host, a port and occasionally a credential.
    """
    global _cached

    now = time.monotonic()
    if _cached is not None and now - _cached[0] < ttl_s:
        return _cached[1]

    try:
        resolved = settings or get_graph_settings()
    except MissingGraphSettings:
        # Not cached: an unconfigured deployment costs nothing to answer, and caching it
        # would outlive a configuration change in a development process.
        return GraphStatus.NOT_CONFIGURED

    status = GraphStatus.DEGRADED
    try:
        driver = get_driver(resolved)
        await asyncio.wait_for(driver.verify_connectivity(), timeout=timeout_s)
        status = GraphStatus.OK
    except (TimeoutError, asyncio.CancelledError):
        status = GraphStatus.DEGRADED
    except Exception:
        status = GraphStatus.DEGRADED

    _cached = (now, status)
    return status
