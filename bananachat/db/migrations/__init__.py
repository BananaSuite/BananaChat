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
from .v13_claude import upgrade as v13_claude
from .v14_community import upgrade as v14_community
from .v15_chat_consumption import upgrade as v15_chat_consumption
from .v16_single_token_budget import upgrade as v16_single_token_budget
from .v17_claude_pool_safety import upgrade as v17_claude_pool_safety
from .v18_limit_controls import upgrade as v18_limit_controls
from .v19_external_providers import upgrade as v19_external_providers
from .v20_worker_offline_warning import upgrade as v20_worker_offline_warning
from .v21_remove_claude_connector import upgrade as v21_remove_claude_connector
from .v22_api_token_scopes import upgrade as v22_api_token_scopes
from .v23_external_provider_presets import upgrade as v23_external_provider_presets
from .v24_two_step import upgrade as v24_two_step
from .v25_run_owner_worker_backoff import upgrade as v25_run_owner_worker_backoff
from .v26_subscription_pool import upgrade as v26_subscription_pool
from .v27_worker_maintenance import upgrade as v27_worker_maintenance
from .v28_device_epoch import upgrade as v28_device_epoch
from .v29_community_kinds import upgrade as v29_community_kinds

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
    v13_claude,
    v14_community,
    v15_chat_consumption,
    v16_single_token_budget,
    v17_claude_pool_safety,
    v18_limit_controls,
    v19_external_providers,
    v20_worker_offline_warning,
    v21_remove_claude_connector,
    v22_api_token_scopes,
    v23_external_provider_presets,
    v24_two_step,
    v25_run_owner_worker_backoff,
    v26_subscription_pool,
    v27_worker_maintenance,
    v28_device_epoch,
    v29_community_kinds,
)

SCHEMA_VERSION = len(MIGRATIONS)
