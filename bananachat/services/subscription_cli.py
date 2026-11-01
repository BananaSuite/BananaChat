"""Pieces the Claude Code and Codex transports share: their error types and the operator's private files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat

MAX_LINE = 1024 * 1024


class ConnectorError(RuntimeError):
    """A credential-free error safe to show in the administrator interface."""


class ConnectorTimeout(ConnectorError):
    """The provider's process ran out of time: the request's length, not necessarily the account (see
    ``subscription_pool.chat_stream``)."""

    timed_out = True


def private(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ConnectorError("Use an absolute, non-symlink private profile path.")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConnectorError("Connector files and profile directories must belong to the service user and be private.")
    if directory != stat.S_ISDIR(info.st_mode):
        raise ConnectorError("The connector path has the wrong file type.")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise ConnectorError("The connector file must be a regular file.")
    return path.resolve()


def json_file(path):
    path = private(path)
    with path.open("rb") as source:
        data = source.read(MAX_LINE + 1)
    if len(data) > MAX_LINE:
        raise ConnectorError("The connector configuration is too large.")
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise ConnectorError("The connector configuration is not valid JSON.") from None
