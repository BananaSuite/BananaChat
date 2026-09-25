"""Site settings."""

from ._connection import get_db_context, retry_on_busy

_ALLOWED_COLUMNS = {
    "site_name", "signup_mode", "maintenance_mode", "maintenance_message", "setup_done",
    "default_daily_credits", "default_slow_credits",
    "warning_banner_enabled", "warning_banner_dismissible", "warning_banner_message",
    "slow_credits_enabled", "api_rpm", "chat_rpm",
    "chat_daily_limit_enabled", "chat_daily_credits", "chat_daily_slow_credits",
    "music_bonus_mode", "music_bonus_fixed_credits", "music_bonus_fixed_slow",
    "quota_auto_approve_enabled", "quota_auto_approve_max_credits",
    "quota_auto_approve_max_slow_credits",
    "default_theme_mode", "primary_color", "secondary_color", "accent_color",
    "text_color", "sidebar_color", "bg_color", "light_primary_color",
    "light_secondary_color", "light_accent_color", "light_text_color",
    "light_sidebar_color", "light_bg_color",
}


@retry_on_busy
def get_site_settings():
    """Return site_settings row as a dict, or None if not initialised."""
    with get_db_context() as conn:
        row = conn.execute("SELECT * FROM site_settings WHERE id=1").fetchone()
    return dict(row) if row else None


def update_site_settings(**kwargs):
    """Update one or more site settings columns."""
    safe = {k: v for k, v in kwargs.items() if k in _ALLOWED_COLUMNS}
    if not safe:
        return
    cols = ", ".join(f"{k}=?" for k in safe)
    with get_db_context() as conn:
        conn.execute(f"UPDATE site_settings SET {cols} WHERE id=1", list(safe.values()))  # noqa: S608
        conn.commit()
