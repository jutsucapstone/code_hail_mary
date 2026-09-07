"""Every connector releases the HTTP client the worker handed it.

`close_connector` looks the method up with `getattr` and does nothing when it is absent,
so a connector without `aclose` leaks its httpx client — and a connection pool with it —
once per job, in a worker process that drains jobs for as long as it is alive. Seven of
the twelve had no such method and inherited a no-op that read like cleanup.

A registry-wide assertion rather than one test per provider: the failure mode is a
connector being *added* without one, and only a test that walks the registry catches that.
"""

from __future__ import annotations

import inspect

from jutsu_connectors.providers import CONNECTOR_CLASSES


class TestEveryConnectorReleasesItsClient:
    def test_every_registered_connector_defines_aclose(self) -> None:
        missing = sorted(
            provider_id
            for provider_id, cls in CONNECTOR_CLASSES.items()
            if not callable(getattr(cls, "aclose", None))
        )

        assert missing == [], f"these connectors would leak their HTTP client: {missing}"

    def test_aclose_is_a_coroutine_the_worker_can_await(self) -> None:
        """`close_connector` awaits the result. A synchronous `aclose` would raise at the
        end of every job rather than closing anything."""
        for provider_id, cls in CONNECTOR_CLASSES.items():
            # `CONNECTOR_CLASSES` is typed `dict[str, type]` — the registry deliberately
            # does not name a protocol, because the four Google products and the three
            # Microsoft ones share bases the worker never refers to. So the lookup is a
            # `getattr`, which is exactly what `close_connector` does at runtime.
            closer = getattr(cls, "aclose", None)
            assert closer is not None and inspect.iscoroutinefunction(closer), provider_id

    def test_the_registry_covers_every_provider_in_the_catalogue(self) -> None:
        """The two lists are the same set, so "every connector" means what it says."""
        from jutsu_core.providers import PROVIDERS

        assert set(CONNECTOR_CLASSES) == set(PROVIDERS)
