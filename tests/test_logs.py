from __future__ import annotations

import json
import logging

from bananachat.logs import JsonFormatter


def _record(message, *args, exc_info=None):
    return logging.LogRecord("bananachat.auth", logging.WARNING, __file__, 1, message, args, exc_info)


def test_json_lines_carry_time_level_logger_and_message():
    entry = json.loads(JsonFormatter().format(_record("Failed sign-in from %s", "203.0.113.9")))
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "bananachat.auth"
    assert entry["message"] == "Failed sign-in from 203.0.113.9"
    assert entry["time"].endswith("+00:00")
    assert "exception" not in entry


def test_json_lines_stay_on_one_line_with_a_traceback():
    try:
        raise ValueError("bad\nvalue")
    except ValueError as error:
        line = JsonFormatter().format(_record("Unhandled error", exc_info=(type(error), error, error.__traceback__)))
    assert "\n" not in line
    assert "ValueError: bad" in json.loads(line)["exception"]
