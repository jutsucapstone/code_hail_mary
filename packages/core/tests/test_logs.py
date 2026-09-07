"""The shared JSON log formatter: one parseable line, whatever the record carries.

Each test here is a defect that reached a deployed service. A quote in a message broke
the line; a dict message became an unqueryable Python repr; `LOG_LEVEL=debug` raised at
startup; and uvicorn's own loggers never went through any of it, so an exception
escaping the ASGI app was logged as plain text outside the JSON stream.
"""

from __future__ import annotations

import json
import logging

import pytest
from jutsu_core.logs import (
    UNBOUND,
    JsonFormatter,
    RedactQueryString,
    configure,
    level_from_env,
)


def record(msg: object = "hello", *args: object, name: str = "jutsu.test") -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, msg, args or None, None)


class TestOneParseableLine:
    def test_a_plain_message_carries_level_logger_and_msg(self) -> None:
        line = json.loads(JsonFormatter().format(record("kt.opened")))
        assert line == {
            "severity": "INFO",
            "level": "INFO",
            "logger": "jutsu.test",
            "message": "kt.opened",
            "msg": "kt.opened",
        }

    def test_the_line_carries_the_two_keys_cloud_logging_actually_reads(self) -> None:
        """`severity` and `message` are promoted out of a structured payload; every
        other key is opaque data. Emitting only `level` and `msg` put every line —
        including a traceback from a failed drain — at DEFAULT severity, so
        `severity>=ERROR` matched nothing and no alert built on it could ever fire."""
        line = json.loads(
            JsonFormatter().format(
                logging.LogRecord(
                    "jutsu.test", logging.ERROR, __file__, 1, "drain_failed", None, None
                )
            )
        )
        assert line["severity"] == "ERROR"
        assert line["message"] == "drain_failed"

    def test_a_structured_event_names_itself_in_the_rendered_summary(self) -> None:
        """The log viewer shows `message`; a dict left there renders as `{...}` and the
        operator has to expand every row to see which event it was."""
        line = json.loads(
            JsonFormatter().format(record("%s", {"event": "sync_started", "org_id": "abc"}))
        )
        assert line["message"] == "sync_started"
        assert line["org_id"] == "abc"

    def test_a_message_containing_quotes_and_newlines_still_parses(self) -> None:
        """The old format string interpolated the message into JSON it had already
        written, so this produced a line Cloud Logging could not parse."""
        hostile = 'he said "no", then\nnewline \\ backslash'
        line = json.loads(JsonFormatter().format(record(hostile)))
        assert line["msg"] == hostile

    def test_a_dict_message_becomes_queryable_fields(self) -> None:
        """`logger.info("%s", {...})` is the convention everywhere in this codebase."""
        line = json.loads(
            JsonFormatter().format(record("%s", {"event": "drain_complete", "jobs": 4}))
        )
        assert line["event"] == "drain_complete"
        assert line["jobs"] == 4
        # The summary stays readable: `event` stands in for the message.
        assert line["msg"] == "drain_complete"

    def test_a_dict_without_an_event_keeps_the_rendered_message(self) -> None:
        line = json.loads(JsonFormatter().format(record("%s", {"jobs": 1})))
        assert line["jobs"] == 1
        assert "jobs" in line["msg"]

    def test_a_value_json_cannot_encode_renders_as_a_string(self) -> None:
        import uuid

        identifier = uuid.uuid4()
        line = json.loads(JsonFormatter().format(record("%s", {"org_id": identifier})))
        assert line["org_id"] == str(identifier)

    def test_context_fields_render_as_a_dash_when_nothing_bound_them(self) -> None:
        line = json.loads(JsonFormatter(context_fields=("request_id",)).format(record()))
        assert line["request_id"] == UNBOUND


class TestExceptions:
    def test_a_traceback_is_a_json_string_field_not_a_second_line(self) -> None:
        """`logging.Formatter` appends the traceback after the formatted record, which
        put non-JSON text on the line following every error."""
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            import sys

            entry = logging.LogRecord(
                "jutsu.test", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
            )
        line = json.loads(JsonFormatter().format(entry))
        assert line["error"] == "RuntimeError"
        assert "RuntimeError: boom" in line["traceback"]


class TestLevel:
    def test_a_lowercase_level_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOG_LEVEL", "debug")
        assert level_from_env() == logging.DEBUG

    def test_an_unknown_level_falls_back_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`logging.basicConfig(level="verbose")` raises ValueError; a worker that did
        that at lifespan start never became Ready."""
        monkeypatch.setenv("LOG_LEVEL", "verbose")
        assert level_from_env() == logging.INFO

    def test_unset_is_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        assert level_from_env() == logging.INFO


class TestConfigure:
    def test_uvicorns_own_loggers_are_taken_over(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """uvicorn sets `propagate = False` and installs its own plain-text handler, so
        an exception escaping the ASGI app bypassed every formatter configured here."""
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        noisy = logging.getLogger("uvicorn.error")
        noisy.handlers = [logging.StreamHandler()]
        noisy.propagate = False

        root_handlers_before = logging.getLogger().handlers
        try:
            configure()
            assert noisy.handlers == []
            assert noisy.propagate is True
            assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)
        finally:
            logging.getLogger().handlers = root_handlers_before

    def test_filters_reach_the_handler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        stamped: list[str] = []

        class Stamp(logging.Filter):
            def filter(self, entry: logging.LogRecord) -> bool:
                stamped.append(entry.name)
                return True

        root_handlers_before = logging.getLogger().handlers
        try:
            handler = configure(filters=(Stamp(),))
            handler.handle(record())
            assert stamped == ["jutsu.test"]
        finally:
            logging.getLogger().handlers = root_handlers_before


class TestTheAccessLineCarriesNoSecrets:
    """Taking uvicorn's loggers over brought its access record into the JSON stream, and
    that record's request target is the full one — query string included.

    Two things ride there that §4.9 forbids in a log: the OAuth authorization `code` and
    `state` on the connector callback, and the free-text `q` of a people search.
    """

    def _access(self, target: str) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", "GET", target, "1.1", 200),
            None,
        )

    def test_an_oauth_callback_code_never_reaches_the_line(self) -> None:
        record = self._access("/v1/connections/callback?code=SENTINEL-code&state=SENTINEL-state")
        assert RedactQueryString().filter(record) is True

        line = json.loads(JsonFormatter().format(record))

        assert "SENTINEL" not in line["message"]
        assert "/v1/connections/callback" in line["message"], "the path is still useful"
        assert "<redacted>" in line["message"]

    def test_a_search_term_never_reaches_the_line(self) -> None:
        record = self._access("/v1/employees?q=ada%20lovelace&limit=50")
        RedactQueryString().filter(record)

        assert "lovelace" not in json.loads(JsonFormatter().format(record))["message"]

    def test_a_target_with_no_query_is_untouched(self) -> None:
        record = self._access("/v1/me")
        RedactQueryString().filter(record)

        assert "/v1/me" in json.loads(JsonFormatter().format(record))["message"]
        assert "<redacted>" not in json.loads(JsonFormatter().format(record))["message"]

    def test_it_leaves_every_other_logger_alone(self) -> None:
        """The filter sits on the shared handler, so it must key on the logger name —
        an application event whose message happens to contain a `?` is not an access
        line and must not be rewritten."""
        record = logging.LogRecord(
            "jutsu.api", logging.INFO, __file__, 1, "who? nobody", None, None
        )
        RedactQueryString().filter(record)

        assert json.loads(JsonFormatter().format(record))["message"] == "who? nobody"
