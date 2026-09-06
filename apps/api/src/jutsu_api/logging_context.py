"""Request context on every log line — non-negotiable 9, made structural.

The API always logged structured JSON, but the formatter emitted only `level`, `logger`
and `msg`. `request_id` was placed on `request.state`, passed as `extra=` at one call
site, and silently dropped because the format string never named it — so a person
quoting the "Reference" id from an error screen could not be found in Cloud Run's logs.
Non-negotiable 9 asks for `trace_id`, `org_id` and an opaque `user_id` on each line.

The mechanism is a `contextvars.ContextVar` bound per request and a `logging.Filter` on
the one handler that stamps its values onto every record, whatever module emitted it.
A contextvar rather than `extra=`: `extra` has to be remembered at every call, and the
one place that remembered it was the one place it did nothing.

What is bound: the request id (a UUID minted or honoured by the middleware), the org id,
and the user id — both UUIDs, both opaque, neither an email, a name or a subject. Nothing
else may be bound here; `FIELDS` is the whole vocabulary, and `bind` drops anything not
in it rather than letting a caller widen the log line.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Final

__all__ = ["FIELDS", "UNBOUND", "RequestContextFilter", "bind", "clear", "current"]

#: The only fields a log line may carry from the request. Keys, not values — a caller
#: cannot add one.
FIELDS: Final = ("request_id", "org_id", "user_id")

#: What an unbound field renders as. A dash, not an empty string, so a line read by a
#: person shows that the field exists and had no value — the readiness probes, say.
UNBOUND: Final = "-"

_context: ContextVar[dict[str, str] | None] = ContextVar("jutsu_log_context", default=None)


def bind(**values: str) -> None:
    """Attach request context for the current task. Unknown keys are dropped."""
    merged = dict(_context.get() or {})
    for key, value in values.items():
        if key in FIELDS:
            merged[key] = str(value)
    _context.set(merged)


def clear() -> None:
    """Forget the current task's context. Called at the start of every request so a
    pooled task never inherits the previous request's identity."""
    _context.set({})


def current() -> dict[str, str]:
    return dict(_context.get() or {})


class RequestContextFilter(logging.Filter):
    """Stamp the bound context onto every record that passes the handler.

    A filter on the *handler* sees every record from every logger, which is the point:
    `jutsu.retrieval.search` and `jutsu.api.kt` alike get the request id without knowing
    the mechanism exists. It never rejects a record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        context = _context.get() or {}
        for field in FIELDS:
            setattr(record, field, context.get(field, UNBOUND))
        return True
