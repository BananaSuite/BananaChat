"""Administration of the music program: settings, tracks and participants."""

from __future__ import annotations

import logging
import math
import os
import tempfile
import uuid
from pathlib import Path

from flask import current_app, flash, redirect, render_template, request, url_for

from bananachat import db, security
from bananachat.db import credits
from bananachat.db import limits as limits_db
from bananachat.db import music as music_db
from bananachat.db import settings as site_settings
from bananachat.db import users

from . import bp

log = logging.getLogger("bananachat.admin.music")

MAX_TRACK_BYTES = 50 * 1024 * 1024
UPLOAD_LIMIT = MAX_TRACK_BYTES + 256 * 1024
PAGE_SIZE = 50
MULTIPLIER_RANGE = (1.0, 10.0)
FIXED_MAX = limits_db.TOKENS_MAX  # tokens per 5-hour window
WEEKLY_FIXED_MAX = 7 * FIXED_MAX


def _redirect(anchor: str | None = None, **params):
    return redirect(url_for("admin.music", _anchor=anchor, **params))


def _audit(action: str, target: str = "", details=None) -> None:
    users.audit(security.current_user(), f"music.{action}", target, details, security.client_ip())


# ----- audio validation -----------------------------------------------------

def sniff_audio(head: bytes) -> str | None:
    """The audio container of a file from its first bytes: mp3, ogg, wav, m4a or None."""
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    if head[4:8] == b"ftyp":
        return "m4a"
    return None


def _store_upload(upload, extension: str) -> tuple[str, Path]:
    directory = current_app.config["BC"].audio_dir
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".upload-", suffix=".tmp")
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while True:
                chunk = upload.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_TRACK_BYTES:
                    raise ValueError("too_large")
                handle.write(chunk)
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        filename = f"{uuid.uuid4().hex}.{extension}"
        final = directory / filename
        os.replace(temporary, final)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return filename, final


# ----- page -----------------------------------------------------------------

@bp.get("/music", endpoint="music")
@security.admin_required
def music():
    settings = site_settings.get()
    search = (request.args.get("q") or "").strip()[:64]
    status = request.args.get("status") if request.args.get("status") in music_db.PARTICIPANT_FILTERS else "all"
    total = music_db.count_users(search=search, status=status)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    try:
        page = min(max(1, int(request.args.get("page", 1))), pages)
    except ValueError:
        page = 1
    opted, forced = music_db.count_participants()
    return render_template(
        "admin/music.html",
        section="music",
        music=settings,
        tracks=music_db.list_tracks(),
        participants=music_db.list_users(search=search, status=status, limit=PAGE_SIZE,
                                         offset=(page - 1) * PAGE_SIZE),
        total=total, page=page, pages=pages, search=search, status=status,
        opted_count=opted, forced_count=forced,
        max_track_mb=MAX_TRACK_BYTES // (1024 * 1024),
        allowed_types=", ".join(f".{ext}" for ext in music_db.AUDIO_TYPES),
        multiplier_range=MULTIPLIER_RANGE, fixed_max=FIXED_MAX, fixed=credits.music_fixed(settings),
        weekly_fixed_max=WEEKLY_FIXED_MAX, weekly_fixed=credits.music_weekly_fixed(settings),
    )


# ----- settings -------------------------------------------------------------

def _number(name: str, low: float, high: float, *, integer: bool):
    raw = (request.form.get(name) or "").strip()
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        raise ValueError(f"{name.replace('_', ' ').capitalize()} must be a number.") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name.replace('_', ' ').capitalize()} must be between {low:g} and {high:g}.")
    return value


def _tokens(name: str, *, maximum: int = FIXED_MAX) -> int:
    from bananachat.formatting import parse_amount

    value = parse_amount(request.form.get(name))
    if value is None or not 0 <= value <= maximum:
        raise ValueError(f"{name.replace('_', ' ').capitalize()}: write a number of tokens up to "
                         f"{maximum:,}, such as 30000 or 30k.")
    return int(round(value))


@bp.post("/music/settings", endpoint="music_settings")
@security.admin_required
def music_settings():
    form = request.form
    try:
        bonus_mode = form.get("music_bonus_mode") or "multiplier"
        if bonus_mode not in ("multiplier", "fixed"):
            raise ValueError("Unknown bonus mode.")
        playback = form.get("music_playback_mode") or "sequential"
        if playback not in ("sequential", "shuffle"):
            raise ValueError("Unknown playback mode.")
        values = {
            "music_enabled": int(form.get("music_enabled") == "1"),
            "music_visible": int(form.get("music_visible") == "1"),
            "music_opt_in_allowed": int(form.get("music_opt_in_allowed") == "1"),
            "music_opt_out_allowed": int(form.get("music_opt_out_allowed") == "1"),
            "music_playback_mode": playback,
            "music_bonus_mode": bonus_mode,
            "music_credit_multiplier": _number("music_credit_multiplier", *MULTIPLIER_RANGE, integer=False),
            "music_bonus_fixed_tokens": _tokens("music_bonus_fixed_tokens"),
            "music_bonus_fixed_weekly_tokens": (
                _tokens("music_bonus_fixed_weekly_tokens", maximum=WEEKLY_FIXED_MAX)
                if "music_bonus_fixed_weekly_tokens" in form else credits.music_weekly_fixed(site_settings.get())
            ),
        }
    except ValueError as error:
        flash(str(error), "error")
        return _redirect("settings")
    site_settings.update(**values)
    _audit("settings", details=values)
    flash("Music program settings saved.", "success")
    return _redirect("settings")


@bp.post("/music/hide", endpoint="music_hide")
@security.admin_required
def music_hide():
    site_settings.update(music_visible=0)
    _audit("hide")
    flash("The program is hidden. Participants keep their bonus and can still leave from the Free quota page.", "info")
    return _redirect("settings")


@bp.post("/music/remove", endpoint="music_remove")
@security.admin_required
def music_remove():
    count = music_db.remove_program()
    _audit("remove", details={"opted_out": count})
    flash(f"Program removed: it is disabled and {count} account(s) were opted out.", "info")
    return _redirect("settings")


# ----- tracks ---------------------------------------------------------------

@bp.post("/music/tracks", endpoint="music_upload")
@security.body_limit(UPLOAD_LIMIT)
@security.admin_required
def music_upload():
    upload = request.files.get("track")
    if upload is None or not upload.filename:
        flash("Choose an audio file to upload.", "error")
        return _redirect("tracks")
    original = upload.filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, extension = original.rpartition(".")
    extension = extension.lower() if dot else ""
    if extension not in music_db.AUDIO_TYPES:
        flash(f"Unsupported file type. Allowed: {', '.join(music_db.AUDIO_TYPES)}.", "error")
        return _redirect("tracks")
    head = upload.stream.read(16)
    detected = sniff_audio(head)
    if detected != extension:
        flash("The file content does not match its extension, or it is not an audio file.", "error")
        return _redirect("tracks")
    upload.stream.seek(0)
    display_name = music_db.clean_display_name(request.form.get("display_name") or stem) or "Untitled"
    try:
        filename, path = _store_upload(upload, extension)
    except ValueError:
        flash(f"The file is larger than {MAX_TRACK_BYTES // (1024 * 1024)} MB.", "error")
        return _redirect("tracks")
    try:
        track_id = music_db.add_track(filename, display_name, security.current_user()["id"])
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    _audit("track_upload", str(track_id), {"name": display_name, "file": filename})
    flash(f"Track “{display_name}” uploaded.", "success")
    return _redirect("tracks")


@bp.post("/music/tracks/<int:track_id>/rename", endpoint="music_rename")
@security.admin_required
def music_rename(track_id):
    try:
        renamed = music_db.rename_track(track_id, request.form.get("display_name") or "")
    except ValueError:
        flash("A track needs a name.", "error")
        return _redirect("tracks")
    if not renamed:
        flash("Track not found.", "error")
    else:
        _audit("track_rename", str(track_id), {"name": music_db.clean_display_name(request.form["display_name"])})
        flash("Track renamed.", "success")
    return _redirect("tracks")


@bp.post("/music/tracks/<int:track_id>/move", endpoint="music_move")
@security.admin_required
def music_move(track_id):
    offset = -1 if request.form.get("direction") == "up" else 1
    if music_db.move_track(track_id, offset):
        _audit("track_move", str(track_id), {"direction": "up" if offset < 0 else "down"})
        flash("Track order saved.", "success")
    else:
        flash("That track cannot move further.", "info")
    return _redirect(f"track-{track_id}")


@bp.post("/music/tracks/<int:track_id>/delete", endpoint="music_delete")
@security.admin_required
def music_delete(track_id):
    track = music_db.get_track(track_id)
    filename = music_db.delete_track(track_id)
    if filename is None:
        flash("Track not found.", "error")
        return _redirect("tracks")
    path = current_app.config["BC"].audio_dir / filename
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("Could not delete audio file %s", filename, exc_info=True)
    _audit("track_delete", str(track_id), {"name": track["display_name"] if track else "", "file": filename})
    flash("Track deleted.", "success")
    return _redirect("tracks")


# ----- participants ---------------------------------------------------------

def _participants_redirect():
    params = {key: request.form.get(key) for key in ("q", "status", "page") if request.form.get(key)}
    return _redirect("participants", **params)


@bp.post("/music/users/<user_id>/<action>", endpoint="music_participant")
@security.admin_required
def music_participant(user_id, action):
    if action not in ("force-in", "force-out", "release"):
        flash("Unknown action.", "error")
        return _participants_redirect()
    target = users.get(user_id)
    if target is None:
        flash("User not found.", "error")
        return _participants_redirect()
    if action == "force-in":
        music_db.set_participation(user_id, True, forced=True)
        message = f"{target['username']} is enrolled and cannot leave by themselves."
    elif action == "release":
        music_db.set_participation(user_id, bool(target["music_opted_in"]), forced=False)
        message = f"{target['username']} can now leave the program by themselves."
    else:
        music_db.set_participation(user_id, False)
        message = f"{target['username']} was removed from the program."
    _audit(action.replace("-", "_"), user_id, {"username": target["username"]})
    flash(message, "success")
    return _participants_redirect()


@bp.post("/music/opt-in-all", endpoint="music_opt_in_all")
@security.admin_required
def music_opt_in_all():
    with db.transaction():
        count = music_db.opt_in_everyone()
    _audit("opt_in_all", details={"count": count})
    flash(f"{count} account(s) joined the program. They can leave again while leaving is allowed.", "success")
    return _redirect("participants")


@bp.post("/music/opt-out-all", endpoint="music_opt_out_all")
@security.admin_required
def music_opt_out_all():
    count = music_db.opt_out_everyone()
    _audit("opt_out_all", details={"count": count})
    flash(f"{count} account(s) left the program.", "success")
    return _redirect("participants")
