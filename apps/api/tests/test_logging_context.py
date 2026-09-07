"""Request context on every log line (non-negotiable 9).

The API's formatter emitted only level/logger/msg, and the one `extra=` that named a
request id was dropped because the format string never referenced it. These pin the
mechanism that replaced that: a per-task context, a filter on the one handler, and a
format string that names the three opaque fields — and nothing else.
"""

from __future__ import annotations

import asyncio
import json
import logging

from jutsu_api.logging_context import (
    FIELDS,
    UNBOUND,
    RequestContextFilter,
    bind,
    clear,
    current,
)
from jutsu_api.main import _configure_logging


def _record(message: str = "hello") -> logging.LogRecord:
    return logging.LogRecord("jutsu.test", logging.INFO, __file__, 1, message, None, None)


class TestTheFilter:
    def test_unbound_fields_render_as_a_dash_and_never_reject(self) -> None:
        clear()
        record = _record()
        assert RequestContextFilter().filter(record) is True
        for field in FIELDS:
            assert getattr(record, field) == UNBOUND

    def test_bound_fields_are_stamped_on_every_record(self) -> None:
        clear()
        bind(request_id="req-1", org_id="org-1", user_id="user-1")
        record = _record()
        RequestContextFilter().filter(record)
        assert (record.request_id, record.org_id, record.user_id) == ("req-1", "org-1", "user-1")  # type: ignore[attr-defined]

    def test_bind_merges_and_refuses_fields_outside_the_vocabulary(self) -> None:
        """The three fields are the whole vocabulary. An email, a name or a subject
        offered under any other key is dropped, not logged."""
        clear()
        bind(request_id="req-1")
        bind(org_id="org-1", email="ada@example.com", subject="local:ada")
        assert current() == {"request_id": "req-1", "org_id": "org-1"}

    def test_clear_forgets_everything(self) -> None:
        bind(request_id="req-1", org_id="org-1", user_id="user-1")
        clear()
        assert current() == {}

    async def test_two_tasks_do_not_see_each_others_context(self) -> None:
        """Each request runs in its own task; a pooled worker must never inherit the
        previous request's identity. This is what a ContextVar buys over a global."""
        seen: dict[str, str] = {}

        async def request(name: str) -> None:
            clear()
            bind(request_id=name)
            await asyncio.sleep(0)
            seen[name] = current()["request_id"]

        await asyncio.gather(request("a"), request("b"))
        assert seen == {"a": "a", "b": "b"}


class TestTheFormatter:
    def test_the_configured_handler_emits_the_three_fields_as_json(self) -> None:
        """`_configure_logging` installs the filter and a format string that names the
        fields; the line that comes out must parse and carry them."""
        _configure_logging()
        handler = logging.getLogger().handlers[0]
        assert any(isinstance(f, RequestContextFilter) for f in handler.filters)

        clear()
        bind(request_id="req-9", org_id="org-9", user_id="user-9")
        record = _record("kt.opened")
        RequestContextFilter().filter(record)
        line = json.loads(handler.format(record))

        assert line == {
            # `severity` and `message` are the two keys Cloud Logging promotes out of a
            # structured payload; `level` and `msg` are kept beside them because every
            # query, dashboard and grep written against this format uses those names.
            # Emitting only the second pair put every line — a drain's traceback
            # included — at DEFAULT severity, so `severity>=ERROR` matched nothing.
            "severity": "INFO",
            "level": "INFO",
            "logger": "jutsu.test",
            "request_id": "req-9",
            "org_id": "org-9",
            "user_id": "user-9",
            "message": "kt.opened",
            "msg": "kt.opened",
        }

    def test_unbound_context_still_produces_a_parseable_line(self) -> None:
        _configure_logging()
        handler = logging.getLogger().handlers[0]
        clear()
        record = _record("readyz")
        RequestContextFilter().filter(record)
        line = json.loads(handler.format(record))
        assert line["request_id"] == UNBOUND and line["org_id"] == UNBOUND
