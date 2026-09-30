"""The community board: quota requests other people offered to the community, and voting on them.

People support a request (optionally renouncing some tokens of their own
limit in the same scope) or object to it; see :mod:`bananachat.services.community`
for when consent approves a request.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from bananachat import security
from bananachat.db import users
from bananachat.formatting import parse_amount
from bananachat.i18n import translate
from bananachat.services import community

bp = Blueprint("community", __name__)


def _t(key, **params):
    return translate(g.lang, key, **params)


@bp.app_context_processor
def community_context():
    user = getattr(g, "user", None)
    if user is None:
        return {"community_nav_visible": False}
    try:
        options = community.settings(getattr(g, "settings", None))
    except Exception:  # noqa: BLE001 - the navigation never breaks a page
        return {"community_nav_visible": False}
    return {"community_nav_visible": bool(options["enabled"] and options["kinds"])}


def _item(row, user, options, mine: dict) -> dict:
    from bananachat.web.account import request_summary

    consent = community.consent(row, options)
    scope = community.scope(row)
    problem = community.eligible(user, row, options)
    return {"row": row, "summary": request_summary(row), "consent": consent, "scope": scope,
            "vote": mine.get(row["id"]), "problem": problem,
            "room": community.pledge_room(user, row, options) if scope and not problem else 0,
            "suggested": _suggested(consent, scope)}


def _suggested(consent, scope) -> int:
    """An even share of what is still missing (at least one supporter's worth)."""
    if not scope or consent.needed <= consent.pledged:
        return 0
    missing = consent.needed - consent.pledged
    left = max(1, consent.min_supporters - consent.supporters)
    return -(-missing // left)


@bp.get("/community", endpoint="index")
@security.login_required
def index():
    user = security.current_user()
    options = community.settings(g.settings)
    mine = community.my_votes(user["id"])
    items = [_item(row, user, options, mine) for row in community.open_requests()]
    return render_template("community/index.html", items=items, options=options,
                           pledges=community.my_pledges(user["id"]))


def _tokens(value) -> int | None:
    if value in (None, ""):
        return 0
    amount = parse_amount(value)
    if amount is None or amount < 0:
        return None
    return int(round(amount))


@bp.post("/community/<int:request_id>/vote", endpoint="vote")
@security.login_required
@security.rate_limit("community-vote", 60, 3600, per_user=True)
def vote(request_id):
    user = security.current_user()
    stance = request.form.get("stance")
    if stance not in community.STANCES:
        abort(400)
    tokens = _tokens((request.form.get("tokens") or "").strip()) if stance == "support" else 0
    if tokens is None:
        flash(_t("community.vote_tokens"), "error")
        return redirect(url_for("community.index", _anchor=f"request-{request_id}"))
    try:
        outcome = community.vote(user, request_id, stance, tokens)
    except community.VoteError as error:
        params = dict(error.params)
        if "max" in params:
            from bananachat.formatting import tokens_text
            params["max"] = tokens_text(params["max"], g.lang)
        flash(_t(f"community.{error.key}", **params), "error")
        return redirect(url_for("community.index", _anchor=f"request-{request_id}"))
    users.audit(user, f"community.{stance}", str(request_id), {"tokens": tokens, "status": outcome["status"]},
                security.client_ip())
    if outcome["status"] == "approved":
        flash(_t("community.vote_approved"), "success")
    else:
        flash(_t("community.vote_saved"), "success")
    return redirect(url_for("community.index", _anchor=f"request-{request_id}"))


@bp.post("/community/<int:request_id>/withdraw", endpoint="withdraw")
@security.login_required
def withdraw(request_id):
    user = security.current_user()
    if community.withdraw(user, request_id):
        users.audit(user, "community.withdraw", str(request_id), None, security.client_ip())
        flash(_t("community.vote_withdrawn"), "success")
    else:
        flash(_t("community.vote_closed"), "info")
    return redirect(url_for("community.index"))
