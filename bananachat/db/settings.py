"""Site-wide settings (the single ``site_settings`` row) and shared runtime state."""

from __future__ import annotations

import json
import re
import time

from bananachat import db

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

DARK_PALETTE = {"primary": "#e6be32", "secondary": "#1d1d1d", "accent": "#cda624",
                "text": "#ededed", "sidebar": "#181818", "bg": "#141414"}
LIGHT_PALETTE = {"primary": "#8a6500", "secondary": "#ffffff", "accent": "#6f5000",
                 "text": "#202124", "sidebar": "#f4f1e8", "bg": "#faf9f5"}
PALETTE_KEYS = tuple(DARK_PALETTE)

# Every writable column of site_settings. Values are validated by callers. The
# limit columns (api_rpm, chat_rpm, default_*_credits, chat_daily_*) are no longer
# read: the limit policies (db.limits) replaced them in schema version 6.
COLUMNS = frozenset({
    "site_name", "signup_mode", "maintenance_mode", "maintenance_message", "setup_done",
    "default_theme_mode",
    *(f"{key}_color" for key in PALETTE_KEYS), *(f"light_{key}_color" for key in PALETTE_KEYS),
    "default_daily_credits", "default_slow_credits", "slow_credits_enabled",
    "warning_banner_enabled", "warning_banner_dismissible", "warning_banner_message",
    "music_enabled", "music_visible", "music_opt_in_allowed", "music_opt_out_allowed",
    "music_credit_multiplier", "music_playback_mode", "music_bonus_mode",
    "music_bonus_fixed_credits", "music_bonus_fixed_slow",
    "api_rpm", "chat_rpm", "chat_daily_limit_enabled", "chat_daily_credits", "chat_daily_slow_credits",
    "quota_auto_approve_enabled", "quota_auto_approve_max_credits", "quota_auto_approve_max_slow_credits",
    "quota_auto_approve_max_weekly_credits", "limits_usage_reset_at", "limits_weekly_reset_at",
    # Token amounts and reasoning effort (schema version 9); the credit columns above are no longer read.
    "quota_auto_approve_max_tokens", "quota_auto_approve_max_slow_tokens", "quota_auto_approve_max_weekly_tokens",
    "music_bonus_fixed_tokens", "music_bonus_fixed_slow_tokens",
    "effort_gating_enabled", "effort_default_level", "effort_auto_unlock", "effort_auto_active_days",
    "effort_auto_tokens", "effort_auto_period_days", "effort_auto_clean_days", "effort_auto_ceiling",
})


def get() -> dict:
    """The settings row as a plain dict (an empty-but-valid dict if missing)."""
    row = db.one("SELECT * FROM site_settings WHERE id=1")
    return row.to_dict() if row else {"site_name": "BananaChat", "setup_done": 0}


def update(**values) -> None:
    unknown = set(values) - COLUMNS
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
    if not values:
        return
    assignments = ", ".join(f"{name}=?" for name in values)
    db.execute(f"UPDATE site_settings SET {assignments} WHERE id=1", tuple(values.values()))


def palette(settings: dict, mode: str) -> dict:
    """The theme colours for *mode*; anything that is not ``#rrggbb`` falls back to the default."""
    base = LIGHT_PALETTE if mode == "light" else DARK_PALETTE
    prefix = "light_" if mode == "light" else ""
    result = {}
    for key, default in base.items():
        value = settings.get(f"{prefix}{key}_color")
        result[key] = value.lower() if isinstance(value, str) and HEX_COLOR.match(value) else default
    return result


# ----- runtime state shared by all worker processes -------------------------

def state_get(key: str, default=None, *, max_age: float | None = None):
    row = db.one("SELECT value, updated_at FROM runtime_state WHERE key=?", (key,))
    if row is None or (max_age is not None and time.time() - row["updated_at"] > max_age):
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def state_set(key: str, value) -> None:
    db.execute(
        "INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value, separators=(",", ":")), time.time()),
    )


def state_delete(key: str) -> None:
    db.execute("DELETE FROM runtime_state WHERE key=?", (key,))
