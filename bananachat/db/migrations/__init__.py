"""Ordered schema versions. ``PRAGMA user_version`` records how many ran.

Append new versions at the end; never reorder, edit or remove released ones.
Migrations must be additive so an older release's lifecycle manager can still
read a database while restoring a backup.
"""

from .history import v1_baseline, v2_chat_runtime, v3_inference_runtime, v4_chat_history
from .v5_rewrite import upgrade as v5_rewrite
from .v6_limits import upgrade as v6_limits
from .v7_personalities import upgrade as v7_personalities
from .v8_agents import upgrade as v8_agents
from .v9_limits_tokens import upgrade as v9_limits_tokens
from .v10_model_lifecycle import upgrade as v10_model_lifecycle
from .v11_limits_automatic import upgrade as v11_limits_automatic
from .v12_pull_claims import upgrade as v12_pull_claims

# The on-disk identity of BananaChat databases. The value is ASCII "BAIA" and
# dates from when the project was named BananaAI; every existing database
# carries it, so it can never change.
APPLICATION_ID = 0x42414941

MIGRATIONS = (
    v1_baseline,
    v2_chat_runtime,
    v3_inference_runtime,
    v4_chat_history,
    v5_rewrite,
    v6_limits,
    v7_personalities,
    v8_agents,
    v9_limits_tokens,
    v10_model_lifecycle,
    v11_limits_automatic,
    v12_pull_claims,
)

SCHEMA_VERSION = len(MIGRATIONS)
