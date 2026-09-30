"""Music program storage: tracks and per-user participation.

Program settings (enabled, visible, bonus...) are columns of ``site_settings``
and change through :func:`bananachat.db.settings.update`. The quota bonus
itself is computed by :func:`bananachat.db.credits.music_bonus`.

Participation lives on the user row: ``music_opted_in`` and ``music_forced``
(enrolled by an administrator; such users cannot leave by themselves).
"""

from __future__ import annotations

import re

from bananachat import db

MAX_DISPLAY_NAME = 200
AUDIO_TYPES = {"mp3": "audio/mpeg", "ogg": "audio/ogg", "wav": "audio/wav", "m4a": "audio/mp4"}
FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.(mp3|ogg|wav|m4a)$")
PARTICIPANT_FILTERS = ("all", "in", "out", "forced")


# ----- tracks ---------------------------------------------------------------

def list_tracks():
    return db.query("SELECT id, filename, display_name, sort_order, uploaded_by, created_at FROM music_tracks "
                    "ORDER BY sort_order, id")


def get_track(track_id):
    return db.one("SELECT id, filename, display_name, sort_order, uploaded_by, created_at FROM music_tracks "
                  "WHERE id=?", (track_id,))


def get_track_by_filename(filename: str):
    if not filename or not FILENAME_RE.match(filename):
        return None
    return db.one("SELECT id, filename, display_name FROM music_tracks WHERE filename=?", (filename,))


def clean_display_name(name: str) -> str:
    return " ".join((name or "").split())[:MAX_DISPLAY_NAME]


def add_track(filename: str, display_name: str, uploaded_by: str | None) -> int:
    if not FILENAME_RE.match(filename or ""):
        raise ValueError("Invalid track file name.")
    display_name = clean_display_name(display_name)
    if not display_name:
        raise ValueError("A track needs a name.")
    with db.transaction():
        position = db.scalar("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM music_tracks", default=0)
        cursor = db.execute("INSERT INTO music_tracks (filename, display_name, sort_order, uploaded_by, created_at) "
                            "VALUES (?,?,?,?,?)", (filename, display_name, position, uploaded_by, db.now()))
    return cursor.lastrowid


def rename_track(track_id: int, display_name: str) -> bool:
    display_name = clean_display_name(display_name)
    if not display_name:
        raise ValueError("A track needs a name.")
    return db.execute("UPDATE music_tracks SET display_name=? WHERE id=?", (display_name, track_id)).rowcount == 1


def delete_track(track_id: int) -> str | None:
    """Delete a track row; returns its file name so the caller can remove the file."""
    with db.transaction():
        row = db.one("SELECT filename FROM music_tracks WHERE id=?", (track_id,))
        if row is None:
            return None
        db.execute("DELETE FROM music_tracks WHERE id=?", (track_id,))
    return row["filename"]


def move_track(track_id: int, offset: int) -> bool:
    """Move a track up (-1) or down (+1); renumbers the order densely. False when it cannot move."""
    with db.transaction():
        ids = [row["id"] for row in db.query("SELECT id FROM music_tracks ORDER BY sort_order, id")]
        if track_id not in ids:
            return False
        index = ids.index(track_id)
        target = index + offset
        if not 0 <= target < len(ids):
            return False
        ids[index], ids[target] = ids[target], ids[index]
        db.executemany("UPDATE music_tracks SET sort_order=? WHERE id=?",
                       [(position, identifier) for position, identifier in enumerate(ids)])
    return True


def reorder_tracks(ordered_ids) -> None:
    """Apply a complete order; unknown ids are ignored, missing ones keep their relative order at the end."""
    with db.transaction():
        existing = [row["id"] for row in db.query("SELECT id FROM music_tracks ORDER BY sort_order, id")]
        known = set(existing)
        wanted = [identifier for identifier in dict.fromkeys(ordered_ids) if identifier in known]
        chosen = set(wanted)
        final = wanted + [identifier for identifier in existing if identifier not in chosen]
        db.executemany("UPDATE music_tracks SET sort_order=? WHERE id=?",
                       [(position, identifier) for position, identifier in enumerate(final)])


def referenced_filenames() -> set[str]:
    return {row["filename"] for row in db.query("SELECT filename FROM music_tracks")}


# ----- participation --------------------------------------------------------

def participation(user_id: str) -> tuple[bool, bool]:
    """``(opted_in, forced)`` for one user."""
    row = db.one("SELECT music_opted_in, music_forced FROM users WHERE id=?", (user_id,))
    if row is None:
        return False, False
    return bool(row["music_opted_in"]), bool(row["music_forced"])


def set_participation(user_id: str, opted_in: bool, *, forced: bool = False) -> bool:
    return db.execute("UPDATE users SET music_opted_in=?, music_forced=? WHERE id=?",
                      (int(opted_in), int(bool(opted_in and forced)), user_id)).rowcount == 1


def opt_in_everyone() -> int:
    """Enrol every regular account (administrators are never limited, so the bonus means nothing to them)."""
    return db.execute("UPDATE users SET music_opted_in=1 WHERE role='user' AND music_opted_in=0").rowcount


def opt_out_everyone() -> int:
    return db.execute("UPDATE users SET music_opted_in=0, music_forced=0 "
                      "WHERE music_opted_in<>0 OR music_forced<>0").rowcount


def remove_program() -> int:
    """Opt everyone out and switch the program off. Returns how many accounts left it."""
    with db.transaction():
        count = opt_out_everyone()
        db.execute("UPDATE site_settings SET music_enabled=0 WHERE id=1")
    return count


def count_participants() -> tuple[int, int]:
    """``(opted_in, forced)`` totals."""
    row = db.one("SELECT COALESCE(SUM(music_opted_in<>0), 0) AS opted, COALESCE(SUM(music_forced<>0), 0) AS forced "
                 "FROM users")
    return int(row["opted"]), int(row["forced"])


def _user_filter(search: str, status: str) -> tuple[str, list]:
    clauses, params = [], []
    if search:
        clauses.append("username LIKE ? ESCAPE '\\'")
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    if status == "in":
        clauses.append("music_opted_in<>0")
    elif status == "out":
        clauses.append("music_opted_in=0")
    elif status == "forced":
        clauses.append("music_forced<>0")
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def list_users(*, search: str = "", status: str = "all", limit: int = 50, offset: int = 0):
    where, params = _user_filter(search, status)
    return db.query(f"SELECT id, username, role, suspended, music_opted_in, music_forced, last_login_at FROM users "
                    f"{where} ORDER BY username COLLATE NOCASE LIMIT ? OFFSET ?", (*params, limit, offset))


def count_users(*, search: str = "", status: str = "all") -> int:
    where, params = _user_filter(search, status)
    return db.scalar(f"SELECT COUNT(*) FROM users {where}", params, 0)
