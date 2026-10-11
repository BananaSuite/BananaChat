"""Audit log viewer and the private error log."""

from __future__ import annotations

import json

from flask import current_app, render_template, request

from bananachat.db import admin_overview, users
from bananachat.logs import read_error_log
from bananachat.security import admin_required

from . import bp
from ._helpers import page

PAGE_SIZE = 100


def _details(raw: str):
    """Pretty-print JSON details; other text is shown as is."""
    if not raw:
        return ""
    try:
        value = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(value, dict):
        return ", ".join(f"{key}: {_value(key, item)}" for key, item in value.items())
    return raw


def _value(key: str, item) -> str:
    """One detail value in words: no Python reprs (None, True) or JSON lists."""
    if item is None:
        return "no end date" if key in ("until", "ends_at", "expires_at") else "—"
    if isinstance(item, bool):
        return "yes" if item else "no"
    if isinstance(item, list):
        return ", ".join(_value(key, part) for part in item) or "—"
    if isinstance(item, dict):
        return json.dumps(item)
    return str(item)


@bp.get("/audit", endpoint="audit")
@admin_required
def audit_log():
    actions = admin_overview.audit_actions()
    action = request.args.get("action", "")
    if action not in actions:
        action = ""
    current = page(users.count_audit(action), PAGE_SIZE)
    rows = users.list_audit(PAGE_SIZE, current.offset, action)
    return render_template("admin/audit.html", section="audit", rows=rows, actions=actions, action=action,
                           page=current, details=_details)


@bp.get("/errors", endpoint="errors")
@admin_required
def error_log():
    config = current_app.config["BC"]
    content = read_error_log(config, limit=200_000)
    response = current_app.make_response(render_template("admin/errors.html", section="audit", content=content,
                                                          path=str(config.error_log)))
    response.headers["Cache-Control"] = "private, no-store"
    return response
