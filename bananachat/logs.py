"""Logging to stderr (journald under systemd) and an optional private file.

``BC_LOGGING_LEVEL``: ``off`` < ``minimal`` (errors) < ``medium`` (warnings)
< ``verbose`` (informational, default) < ``debug``. Prompts, passwords and
tokens are never logged.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

LEVELS = {"off": logging.CRITICAL + 10, "minimal": logging.ERROR, "medium": logging.WARNING,
          "verbose": logging.INFO, "debug": logging.DEBUG}
_configured = False


class _PrivateRotatingHandler(logging.handlers.RotatingFileHandler):
    """A rotating file readable only by the service account."""

    def _open(self):
        previous = os.umask(0o077)
        try:
            return super()._open()
        finally:
            os.umask(previous)


def configure_logging(config) -> None:
    global _configured
    logger = logging.getLogger("bananachat")
    logger.setLevel(LEVELS.get(config.logging_level, logging.INFO))
    if _configured:
        return
    _configured = True
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    if config.log_file and config.logging_level != "off":
        try:
            Path(config.log_file).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            handler = _PrivateRotatingHandler(config.log_file, maxBytes=5 * 1024 * 1024, backupCount=5,
                                              encoding="utf-8", delay=True)
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        except OSError as error:
            logger.warning("Cannot write the log file %s: %s", config.log_file, error)


def record_exception(config, error: BaseException) -> None:
    """Log an unexpected error and keep its traceback in the private error log."""
    logger = logging.getLogger("bananachat")
    logger.error("Unhandled error: %s: %s", type(error).__name__, error, exc_info=error)
    try:
        path = Path(config.error_log)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
            path.replace(path.with_name(path.name + ".1"))
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(f"--- {datetime.now(timezone.utc).isoformat()} ---\n")
            handle.write("".join(traceback.format_exception(type(error), error, error.__traceback__))[-60000:])
            handle.write("\n")
    except OSError:
        pass


def read_error_log(config, limit: int = 40000) -> str:
    try:
        with open(config.error_log, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
