"""Serialized, atomic schema initialization and version upgrades."""

import config
from sqlite_runtime import serialized_schema
from sqlite_migrations import apply_migrations

from ._connection import get_db_context
from .migrations.legacy import _upgrade_legacy
from .migrations.chat_runtime import upgrade as _upgrade_chat_runtime
from .migrations.inference_runtime import upgrade as _upgrade_inference_runtime
from .migrations.chat_history import upgrade as _upgrade_chat_history


@serialized_schema(lambda: config.DATABASE_PATH)
def init_db():
    """Import legacy storage once; refuse unsupported code/database pairings."""
    with get_db_context() as conn:
        apply_migrations(conn, 0x42414941, (_upgrade_legacy, _upgrade_chat_runtime, _upgrade_inference_runtime, _upgrade_chat_history))
