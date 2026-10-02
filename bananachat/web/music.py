"""The music program ("Free quota"): participants hear music while they use the
site and receive larger token limits in exchange.

* ``/free-quota`` explains the program and the exact bonus, and lets people
  join or leave within the administrator's rules (joins/leaves can be paused;
  people enrolled by an administrator cannot leave by themselves).
* The player (``partials/music_player.html`` + ``js/music-player.js``) is
  shown to participants only. It never starts before the person presses
  "Start music", and it can always be paused, skipped and turned down.
* Audio files are served only to participants and administrators.

When the program is hidden, participants can still reach the page (to leave)
and keep their bonus; everybody else no longer sees it.
"""

from __future__ import annotations

from flask import Blueprint, current_app, flash, g, redirect, render_template, send_from_directory, url_for
from werkzeug.exceptions import NotFound

from bananachat import security
from bananachat.db import credits
from bananachat.db import music as music_db
from bananachat.db import users
from bananachat.i18n import translate
from bananachat.services import limits

bp = Blueprint("music", __name__)

DEFAULT_MULTIPLIER = 2.0


def _t(key, **params):
    return translate(g.lang, key, **params)


def _settings() -> dict:
    return getattr(g, "settings", None) or {}


def offered_bonus(settings: dict) -> tuple[str, float, float]:
    """The bonus the program currently grants, whether or not the viewer takes part.

    Mirrors :func:`bananachat.db.credits.music_bonus` (which answers for one
    participant): ``("multiplier", factor, 0)`` or ``("fixed", tokens, 0)`` per 5-hour window.
    The final, unused slot remains for callers of the released helper.
    """
    if (settings.get("music_bonus_mode") or "multiplier") == "fixed":
        return ("fixed", *credits.music_fixed(settings))
    multiplier = settings.get("music_credit_multiplier")
    multiplier = DEFAULT_MULTIPLIER if multiplier is None else max(0.0, float(multiplier))
    return "multiplier", multiplier, 0.0


def _participant(user) -> bool:
    return bool(user is not None and user["music_opted_in"])


def page_available(user, settings: dict) -> bool:
    """Participants can always reach the page while the program runs; others only while it is visible."""
    if user is None or not settings.get("music_enabled"):
        return False
    return bool(settings.get("music_visible")) or _participant(user)


def player_active(user, settings: dict) -> bool:
    return bool(user is not None and settings.get("music_enabled") and _participant(user))


@bp.app_context_processor
def music_context():
    user = getattr(g, "user", None)
    settings = _settings()
    if user is None:
        return {"music_nav_visible": False, "music_player": None}
    player = None
    if player_active(user, settings):
        tracks = [{"id": row["id"], "name": row["display_name"],
                   "url": url_for("music.audio", filename=row["filename"])} for row in music_db.list_tracks()]
        if tracks:
            player = {
                "tracks": tracks,
                "mode": "shuffle" if settings.get("music_playback_mode") == "shuffle" else "sequential",
                "user": user["id"],
                "page": url_for("music.free_quota"),
            }
    return {"music_nav_visible": page_available(user, settings), "music_player": player}


def _base_limits(user, settings: dict) -> list[dict]:
    """Enabled 5-hour and weekly allowances without and with the bonus, per pool."""
    mode, token_bonus, _ = offered_bonus(settings)
    weekly_bonus = credits.music_weekly_fixed(settings) if mode == "fixed" else token_bonus

    def boosted(value, bonus):
        return value * bonus if mode == "multiplier" else value + bonus

    rows = []
    for pool in limits.visible_pools(user):
        base = limits.base_limits(user["id"], pool)
        for period, bonus in (("window", token_bonus), ("weekly", weekly_bonus)):
            if base[f"{period}_enabled"]:
                tokens = float(base[f"{period}_tokens"])
                rows.append({"pool": pool, "period": period, "tokens": tokens,
                             "tokens_with_bonus": boosted(tokens, bonus)})
    return rows


@bp.get("/free-quota", endpoint="free_quota")
@security.login_required
def free_quota():
    user = security.current_user()
    settings = _settings()
    if not page_available(user, settings):
        flash(_t("music.unavailable"), "info")
        return redirect(url_for("account.index"))
    opted_in, forced = music_db.participation(user["id"])
    mode, token_bonus, _ = offered_bonus(settings)
    allowances = [] if user["role"] == "admin" else _base_limits(user, settings)
    weekly_bonus = credits.music_weekly_fixed(settings) if mode == "fixed" and \
        any(row["period"] == "weekly" for row in allowances) else 0
    return render_template(
        "music/free_quota.html",
        opted_in=opted_in,
        forced=forced,
        bonus_mode=mode,
        bonus_tokens=token_bonus,
        bonus_weekly=weekly_bonus,
        limits=allowances,
        can_join=bool(settings.get("music_opt_in_allowed")) and bool(settings.get("music_visible")),
        can_leave=bool(settings.get("music_opt_out_allowed")) and not forced,
        opt_out_allowed=bool(settings.get("music_opt_out_allowed")),
        track_count=len(music_db.list_tracks()),
        is_admin_user=user["role"] == "admin",
    )


@bp.post("/free-quota/opt-in", endpoint="opt_in")
@security.login_required
@security.rate_limit("music-opt", 20, 3600, per_user=True)
def opt_in():
    user = security.current_user()
    settings = _settings()
    if not settings.get("music_enabled") or not settings.get("music_visible"):
        flash(_t("music.unavailable"), "error")
        return redirect(url_for("account.index"))
    if not settings.get("music_opt_in_allowed"):
        flash(_t("music.join_paused"), "error")
        return redirect(url_for("music.free_quota"))
    opted_in, _forced = music_db.participation(user["id"])
    if not opted_in:
        music_db.set_participation(user["id"], True)
        users.audit(user, "music.opt_in", ip_address=security.client_ip())
    flash(_t("music.joined"), "success")
    return redirect(url_for("music.free_quota"))


@bp.post("/free-quota/opt-out", endpoint="opt_out")
@security.login_required
@security.rate_limit("music-opt", 20, 3600, per_user=True)
def opt_out():
    user = security.current_user()
    settings = _settings()
    opted_in, forced = music_db.participation(user["id"])
    if not settings.get("music_enabled"):
        flash(_t("music.unavailable"), "error")
        return redirect(url_for("account.index"))
    if forced:
        flash(_t("music.forced_cannot_leave"), "error")
        return redirect(url_for("music.free_quota"))
    if not settings.get("music_opt_out_allowed"):
        flash(_t("music.leave_paused"), "error")
        return redirect(url_for("music.free_quota"))
    if opted_in:
        music_db.set_participation(user["id"], False)
        users.audit(user, "music.opt_out", ip_address=security.client_ip())
    flash(_t("music.left"), "success")
    return redirect(url_for("music.free_quota") if settings.get("music_visible") else url_for("account.index"))


@bp.get("/music/audio/<filename>", endpoint="audio")
@security.login_required
def audio(filename):
    user = security.current_user()
    allowed = user["role"] == "admin" or player_active(user, _settings())
    track = music_db.get_track_by_filename(filename) if allowed else None
    if track is None:
        raise NotFound()
    extension = filename.rsplit(".", 1)[1]
    response = send_from_directory(current_app.config["BC"].audio_dir, filename,
                                   mimetype=music_db.AUDIO_TYPES[extension], conditional=True, max_age=3600)
    response.cache_control.private = True
    response.cache_control.public = False
    return response
