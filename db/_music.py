"""Music program: opt-in/out, admin controls, quota multiplier, track management.

The music program is a fun incentive: users who opt in hear music on loop
and receive a credit multiplier on their daily quota.  The feature is
disabled by default and must be enabled by an admin.  Admins upload tracks
and choose sequential or shuffle playback order.
"""

from contextlib import nullcontext

from ._connection import get_db_context, retry_on_busy


def get_music_settings():
    """Return the music-related columns from site_settings as a dict."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT music_enabled, music_visible, music_opt_in_allowed, "
            "music_opt_out_allowed, music_credit_multiplier, music_playback_mode "
            "FROM site_settings WHERE id=1"
        ).fetchone()
    if not row:
        return {
            "music_enabled": 0,
            "music_visible": 1,
            "music_opt_in_allowed": 1,
            "music_opt_out_allowed": 1,
            "music_credit_multiplier": 2.0,
            "music_playback_mode": "sequential",
        }
    return dict(row)


@retry_on_busy
def update_music_settings(**kwargs):
    """Update music program settings (admin only)."""
    _ALLOWED = {
        "music_enabled", "music_visible", "music_opt_in_allowed",
        "music_opt_out_allowed", "music_credit_multiplier", "music_playback_mode",
    }
    safe = {k: v for k, v in kwargs.items() if k in _ALLOWED}
    if not safe:
        return
    cols = ", ".join(f"{k}=?" for k in safe)
    with get_db_context() as conn:
        conn.execute(f"UPDATE site_settings SET {cols} WHERE id=1", list(safe.values()))  # noqa: S608
        conn.commit()


@retry_on_busy
def is_user_music_opted_in(user_id):
    """Return True if the user has opted into the music program."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT music_opted_in FROM users WHERE id=?", (user_id,)
        ).fetchone()
    return bool(row and row["music_opted_in"])


@retry_on_busy
def set_user_music_opt_in(user_id, opted_in, forced=False):
    """Set or clear a user's music opt-in status."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE users SET music_opted_in=?, music_forced=? WHERE id=?",
            (1 if opted_in else 0, 1 if forced else 0, user_id),
        )
        conn.commit()


@retry_on_busy
def is_user_music_forced(user_id):
    """Return True if the admin force-opted this user in."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT music_forced FROM users WHERE id=?", (user_id,)
        ).fetchone()
    return bool(row and row["music_forced"])


@retry_on_busy
def opt_in_all_users():
    """Opt every non-admin user into the music program."""
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE users SET music_opted_in=1 WHERE role='user'"
        )
        conn.commit()
        return cur.rowcount


@retry_on_busy
def opt_out_all_users():
    """Opt every user out of the music program and clear force flags."""
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE users SET music_opted_in=0, music_forced=0"
        )
        conn.commit()
        return cur.rowcount


@retry_on_busy
def remove_program():
    """Disable the program, opt everyone out, reset all flags."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE users SET music_opted_in=0, music_forced=0"
        )
        conn.execute(
            "UPDATE site_settings SET music_enabled=0, music_visible=1, "
            "music_opt_in_allowed=1, music_opt_out_allowed=1 WHERE id=1"
        )
        conn.commit()


@retry_on_busy
def count_music_opted_in():
    """Return the number of users currently opted into the music program."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE music_opted_in=1"
        ).fetchone()
    return row["c"] if row else 0


def get_effective_quota_multiplier(user_id):
    """Return the credit multiplier for a user (1.0 if not opted in).

    Legacy convenience wrapper: use get_effective_quota_bonus() for the
    full multiplier-or-fixed result.
    """
    mode, val_reg, _ = get_effective_quota_bonus(user_id)
    return val_reg if mode == "multiplier" else 1.0


def get_effective_quota_bonus(user_id, *, connection=None):
    """Return (mode, value_reg, value_slow) for the music program bonus.

    mode='none': user not opted in or program disabled → (1.0, 0)
    mode='multiplier': (multiplier, multiplier)
    mode='fixed': (fixed_credits, fixed_slow_credits)
    """
    with (get_db_context() if connection is None else nullcontext(connection)) as conn:
        user_row = conn.execute(
            "SELECT music_opted_in FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not user_row or not user_row["music_opted_in"]:
            return "none", 1.0, 0

        settings_row = conn.execute(
            "SELECT music_enabled, music_credit_multiplier, "
            "music_bonus_mode, music_bonus_fixed_credits, music_bonus_fixed_slow "
            "FROM site_settings WHERE id=1"
        ).fetchone()
        if not settings_row or not settings_row["music_enabled"]:
            return "none", 1.0, 0

        bonus_mode = settings_row.get("music_bonus_mode") or "multiplier"
        if bonus_mode == "fixed":
            return "fixed", int(settings_row.get("music_bonus_fixed_credits") or 30), \
                   int(settings_row.get("music_bonus_fixed_slow") or 15)
        else:
            m = float(settings_row.get("music_credit_multiplier") or 2.0)
            return "multiplier", m, m


@retry_on_busy
def list_music_tracks():
    """Return all tracks ordered by sort_order."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM music_tracks ORDER BY sort_order ASC, id ASC"
        ).fetchall()


@retry_on_busy
def get_music_track(track_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM music_tracks WHERE id=?", (track_id,)
        ).fetchone()


@retry_on_busy
def add_music_track(filename, display_name, uploaded_by=None):
    """Insert a new track. Returns the new row id."""
    with get_db_context() as conn:
        row = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 AS next_order FROM music_tracks").fetchone()
        next_order = row["next_order"] if row else 0
        cur = conn.execute(
            "INSERT INTO music_tracks (filename, display_name, sort_order, uploaded_by) VALUES (?,?,?,?)",
            (filename, display_name, next_order, uploaded_by),
        )
        conn.commit()
        return cur.lastrowid


@retry_on_busy
def rename_music_track(track_id, display_name):
    with get_db_context() as conn:
        conn.execute(
            "UPDATE music_tracks SET display_name=? WHERE id=?",
            (display_name, track_id),
        )
        conn.commit()


@retry_on_busy
def delete_music_track(track_id):
    """Delete a track record and return its filename for disk cleanup."""
    with get_db_context() as conn:
        row = conn.execute("SELECT filename FROM music_tracks WHERE id=?", (track_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM music_tracks WHERE id=?", (track_id,))
        conn.commit()
        return row["filename"]


@retry_on_busy
def reorder_music_tracks(ordered_ids):
    """Set sort_order for tracks based on the position in ordered_ids list."""
    with get_db_context() as conn:
        for idx, track_id in enumerate(ordered_ids):
            conn.execute(
                "UPDATE music_tracks SET sort_order=? WHERE id=?",
                (idx, track_id),
            )
        conn.commit()
