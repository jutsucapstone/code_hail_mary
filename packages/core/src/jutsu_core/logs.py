"""One JSON line per log record, for every JUTSU process (non-negotiable 9).

Three defects this replaces, all of them shapes that only appear in production:

* **A format string cannot escape its own message.** The previous formatters were
  `'{"level":"%(levelname)s",…,"msg":"%(message)s"}'`, so a message containing a quote
  or a newline produced a line that is not JSON, and Cloud Logging kept it as an
  unparsed blob — exactly when something is going wrong and the line matters most.
  `json.dumps` cannot emit an unbalanced string.
* **The codebase logs dicts.** `logger.info("%s", {"event": …})` is the convention
  everywhere, and `"%s"` rendered it to a Python repr: single quotes, `None` instead of
  `null`, unqueryable. Those keys are merged into the JSON object instead, so
  `jsonPayload.event="drain_complete"` is a filter rather than a substring search.
* **uvicorn owns its own loggers.** `uvicorn.error` and `uvicorn.access` install
  handlers and set `propagate = False`, so nothing configured here ever reached them:
  an exception escaping an ASGI app was logged by uvicorn as a plain-text traceback
  outside the JSON stream. `configure` takes those loggers over.

**Tracebacks stay, and are safe to keep because of `hide_parameters=True` on the
engine** (`jutsu_db.engine`): a SQLAlchemy error's text would otherwise carry the bound
parameters of the failing statement — for the documents INSERT, a slice of the document
body and the author's address. With parameters hidden, a traceback names code, not
content, so it is carried as a JSON string field rather than dropped.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterable, Sequence

__all__ = [
    "UNBOUND",
    "JsonFormatter",
    "RedactQueryString",
    "configure",
    "level_from_env",
]

#: What a context field renders as when nothing bound it. Never an empty string, which
#: is indistinguishable from "bound to nothing" in a log query.
UNBOUND = "-"

#: uvicorn's loggers do not propagate by default, so they must be taken over by name.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class JsonFormatter(logging.Formatter):
    """A record as one JSON object.

    `context_fields` are attribute names a filter has stamped on the record (the API's
    `request_id` / `org_id` / `user_id`); they are read with `UNBOUND` as the default so
    a record emitted outside a request still produces every key.
    """

    def __init__(self, *, context_fields: Sequence[str] = ()) -> None:
        super().__init__()
        self._context_fields = tuple(context_fields)

    def format(self, record: logging.LogRecord) -> str:
        # **`severity` and `message`, because those are the two keys the destination
        # reads.** Cloud Logging promotes a fixed set of fields out of a structured
        # payload and treats everything else as opaque data: without `severity` every
        # line — including a stack trace from a failed drain — is ingested at DEFAULT,
        # so `severity>=ERROR` matches nothing, error reporting sees nothing, and an
        # alerting policy built on log severity is silent by construction. `message` is
        # what it renders as the line's summary in the log viewer; `msg` renders as
        # `{...}`. Both spellings are kept so a query, a dashboard or a `grep` written
        # against the old names still resolves.
        payload: dict[str, object] = {
            "severity": record.levelname,
            "level": record.levelname,
            "logger": record.name,
        }
        for field in self._context_fields:
            payload[field] = str(getattr(record, field, UNBOUND))

        rendered = record.getMessage()
        structured = self._structured_args(record)
        if structured is None:
            summary = rendered
        else:
            # A readable summary AND queryable fields. `event` is this codebase's label
            # for what happened, so it stands in for the message when one is present.
            summary = str(structured.get("event", rendered))
            for key, value in structured.items():
                payload.setdefault(str(key), value)
        payload["message"] = summary
        payload["msg"] = summary

        if record.exc_info:
            exc_type = record.exc_info[0]
            payload["error"] = exc_type.__name__ if exc_type is not None else "Exception"
            payload["traceback"] = self.formatException(record.exc_info)

        # `default=str` rather than a raising encoder: a log line must never be the thing
        # that fails. Non-JSON values (UUIDs, datetimes) render as their string form.
        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def _structured_args(record: logging.LogRecord) -> dict[str, object] | None:
        """The dict behind `logger.info("%s", {...})`, or None for a plain message."""
        if record.msg != "%s":
            return None
        args = record.args
        if isinstance(args, dict):  # logging unwraps a lone dict argument
            return dict(args)
        if isinstance(args, tuple) and len(args) == 1 and isinstance(args[0], dict):
            return dict(args[0])
        return None


class RedactQueryString(logging.Filter):
    """Keep uvicorn's access line, drop the part of it that carries secrets.

    Taking uvicorn's loggers over put its access record into the JSON stream, and that
    record's request target is the *full* one — query string included. Two things ride
    there that must never reach a log (§4.9): the OAuth authorization `code` and `state`
    on the connector callback, and the free-text `q` of a people or evidence search.

    Silencing `uvicorn.access` outright would remove the only per-request line the
    services emit on success, so the path is kept and everything after the `?` is
    replaced. The record's args are uvicorn's own positional tuple —
    `(client, method, target, http_version, status)` — and only the target is touched.

    **This does not cover Cloud Run's own request log**, which Google writes outside the
    container and which carries the full URL. Excluding that is a logging-sink
    configuration, not something application code can reach.
    """

    _ACCESS_LOGGER = "uvicorn.access"
    _TARGET = 2

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != self._ACCESS_LOGGER:
            return True
        args = record.args
        if not isinstance(args, tuple) or len(args) <= self._TARGET:
            return True
        target = args[self._TARGET]
        if not isinstance(target, str) or "?" not in target:
            return True
        path, _, _ = target.partition("?")
        record.args = (*args[: self._TARGET], f"{path}?<redacted>", *args[self._TARGET + 1 :])
        return True


def level_from_env(default: int = logging.INFO) -> int:
    """`LOG_LEVEL`, matched case-insensitively against logging's own level names.

    Unset or unrecognised falls back rather than raising: a typo in an environment
    variable must not take a service down at startup, and must not silence it either.
    `logging.basicConfig(level="debug")` raises `ValueError`, which is how the worker
    would have failed to become Ready on an operator's lowercase override.
    """
    requested = os.environ.get("LOG_LEVEL", "").strip().upper()
    return logging.getLevelNamesMapping().get(requested, default)


def configure(
    *,
    context_fields: Sequence[str] = (),
    filters: Iterable[logging.Filter] = (),
) -> logging.Handler:
    """Point the root logger — and uvicorn's — at one JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactQueryString())
    for log_filter in filters:
        handler.addFilter(log_filter)
    handler.setFormatter(JsonFormatter(context_fields=context_fields))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level_from_env())

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    return handler
