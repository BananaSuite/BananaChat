"""Access rules: capabilities, category and model policies, allow/deny lists and access requests."""

from __future__ import annotations

from flask import abort, flash, render_template, request

from bananachat import db
from bananachat.db import access as access_db
from bananachat.db import catalog, users
from bananachat.security import admin_required

from . import bp
from ._helpers import FormError, audit, back, choice, flag, me, text, utc_datetime, utc_input_min

MODE_LABELS = {
    "allow_all": "Everyone",
    "deny_except_allowlist": "Only people on the allowlist",
    "allow_except_denylist": "Everyone except people on the denylist",
}
MODE_HELP = {
    "allow_all": "Every user can use it. The lists below are kept but have no effect.",
    "deny_except_allowlist": "Nobody can use it unless you add them to the allowlist (or approve their request).",
    "allow_except_denylist": "Every user can use it, except the people you add to the denylist.",
}
CAPABILITY_HELP = {
    "uncensored": "Models marked “Uncensored” on their edit page.",
    "image_generation": "Creating images with ComfyUI models.",
    "custom_personality": "Creating and using custom personalities in the chat.",
    "agents": "Running coding agents in cloud sandboxes (Agents). Also needs the feature enabled in Admin → Agents.",
}


def _resource(scope: str, resource_id: int) -> dict:
    """Name and description of a policy's resource; 404 when it does not exist."""
    try:
        resource_id = access_db.check_key(scope, resource_id)
    except ValueError:
        abort(404)
    if scope in access_db.CAPABILITY_SCOPES:
        return {"scope": scope, "id": 0, "name": access_db.SCOPE_LABELS[scope], "kind": "Capability",
                "description": CAPABILITY_HELP[scope]}
    if scope == "category":
        row = catalog.get_category(resource_id)
        return {"scope": scope, "id": resource_id, "name": row["name"], "kind": "Category",
                "description": row["description"] or "Models in this category."}
    row = catalog.get(resource_id)
    return {"scope": scope, "id": resource_id, "name": row["display_name"], "kind": "Model",
            "description": row["ollama_name"]}


@bp.get("/access", endpoint="access")
@admin_required
def overview():
    policies = access_db.all_policies()
    counts = access_db.membership_counts()

    def policy(scope, resource_id):
        return policies.get((scope, resource_id)) or access_db.default_policy(scope, resource_id)

    capabilities = [{"scope": scope, "name": access_db.SCOPE_LABELS[scope], "description": CAPABILITY_HELP[scope],
                     "policy": policy(scope, 0), "counts": counts.get((scope, 0), {})}
                    for scope in access_db.CAPABILITY_SCOPES]
    categories = [{"row": row, "policy": policy("category", row["id"]), "counts": counts.get(("category", row["id"]), {})}
                  for row in catalog.list_categories()]
    models = [{"row": row, "policy": policy("model", row["id"]), "counts": counts.get(("model", row["id"]), {})}
              for row in catalog.list_models()]
    return render_template("admin/access.html", section="access", capabilities=capabilities, categories=categories,
                           models=models, pending=access_db.list_requests(status="pending"),
                           recent=[row for row in access_db.list_requests(status=None, limit=30)
                                   if row["status"] != "pending"][:15],
                           mode_labels=MODE_LABELS, scope_labels=access_db.SCOPE_LABELS)


@bp.get("/access/<scope>/<int:resource_id>", endpoint="access_detail")
@admin_required
def detail(scope, resource_id):
    resource = _resource(scope, resource_id)
    members = access_db.list_memberships(scope, resource["id"])
    now = db.now()
    lists = {"allowlist": [], "denylist": []}
    for row in members:
        lists[row["list_type"]].append({"row": row, "expired": bool(row["expires_at"] and row["expires_at"] <= now)})
    return render_template("admin/access_detail.html", section="access", resource=resource,
                           policy=access_db.get_policy(scope, resource["id"]), lists=lists,
                           requests=[row for row in access_db.list_requests(status=None, limit=500)
                                     if row["scope"] == scope and row["resource_id"] == resource["id"]][:50],
                           mode_labels=MODE_LABELS, mode_help=MODE_HELP, min_datetime=utc_input_min())


@bp.post("/access/<scope>/<int:resource_id>/policy", endpoint="access_policy")
@admin_required
def set_policy(scope, resource_id):
    resource = _resource(scope, resource_id)
    try:
        mode = choice("mode", access_db.MODES, label="Who may use it")
        requests_enabled = flag("requests_enabled")
        access_db.set_policy(scope, resource["id"], mode, requests_enabled, me()["id"])
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.access_detail", scope=scope, resource_id=resource["id"])
    audit("access_policy", f"{scope}:{resource['id']}", {"name": resource["name"], "mode": mode,
                                                          "requests": requests_enabled})
    flash(f"Access to {resource['name']}: {MODE_LABELS[mode].lower()}.", "success")
    return back("admin.access_detail", scope=scope, resource_id=resource["id"])


@bp.post("/access/<scope>/<int:resource_id>/members", endpoint="access_member_add")
@admin_required
def add_member(scope, resource_id):
    resource = _resource(scope, resource_id)
    try:
        username = text("username", max_length=32, required=True, label="Username")
        user = users.get_by_username(username)
        if user is None:
            raise FormError(f"There is no user called {username}.")
        list_type = choice("list_type", access_db.LIST_TYPES, label="List")
        reason = text("reason", max_length=500, label="Reason")
        expires = utc_datetime("expires_at", label="Expiry")
        access_db.add_membership(scope, resource["id"], user["id"], list_type, added_by=me()["id"], reason=reason,
                                 expires_at=db.timestamp(expires) if expires else None)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.access_detail", scope=scope, resource_id=resource["id"])
    audit("access_member_add", f"{scope}:{resource['id']}",
          {"user": user["username"], "list": list_type, "expires": db.timestamp(expires) if expires else None})
    flash(f"{user['username']} added to the {list_type} of {resource['name']}.", "success")
    return back("admin.access_detail", scope=scope, resource_id=resource["id"])


@bp.post("/access/<scope>/<int:resource_id>/members/remove", endpoint="access_member_remove")
@admin_required
def remove_member(scope, resource_id):
    resource = _resource(scope, resource_id)
    list_type = request.form.get("list_type")
    user = users.get(request.form.get("user_id") or "")
    if user is None or list_type not in access_db.LIST_TYPES:
        abort(400)
    access_db.remove_membership(scope, resource["id"], user["id"], list_type)
    audit("access_member_remove", f"{scope}:{resource['id']}", {"user": user["username"], "list": list_type})
    flash(f"{user['username']} removed from the {list_type} of {resource['name']}.", "success")
    return back("admin.access_detail", scope=scope, resource_id=resource["id"])


@bp.post("/access/requests/<int:request_id>", endpoint="access_request_resolve")
@admin_required
def resolve_request(request_id):
    row = db.one("SELECT r.*, u.username FROM model_access_requests r JOIN users u ON u.id=r.user_id WHERE r.id=?",
                 (request_id,))
    if row is None:
        abort(404)
    decision = request.form.get("decision")
    if decision not in ("approve", "deny"):
        abort(400)
    try:
        message = text("message", max_length=1000, label="Message")
        access_db.resolve_request(request_id, me()["id"], decision == "approve", message)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return _after_request(row)
    audit(f"access_request_{decision}", f"{row['scope']}:{row['resource_id']}",
          {"user": row["username"], "request": request_id})
    flash(f"Request from {row['username']} {'approved' if decision == 'approve' else 'denied'}.", "success")
    return _after_request(row)


def _after_request(row):
    if request.form.get("return") == "detail":
        return back("admin.access_detail", scope=row["scope"], resource_id=row["resource_id"])
    return back("admin.access", _anchor="requests")
