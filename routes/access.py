"""Admin access-policy management and user access requests."""

from datetime import datetime, timezone

from flask import flash, redirect, render_template, request, url_for

import db
from helpers import admin_required, get_current_user, login_required
from logger import log_action
from services import model_access


MODE_LABELS = {
    "allow_all": "Allow all",
    "deny_except_allowlist": "No one except",
    "allow_except_denylist": "Everyone except",
}
SCOPE_LABELS = {
    "uncensored": "Capability",
    "image_generation": "Capability",
    "custom_personality": "Capability",
    "category": "Category",
    "model": "Model",
}
ADMIN_ACTIONS = {
    "set_policy", "add_membership", "remove_membership", "resolve_request",
}


def _policies_with_labels():
    policies = []
    for row in db.list_access_policies():
        item = dict(row)
        item["scope_label"] = SCOPE_LABELS[item["scope"]]
        item["mode_label"] = MODE_LABELS[item["mode"]]
        policies.append(item)
    return policies


def _policy_map(policies=None):
    policies = policies if policies is not None else _policies_with_labels()
    return {(item["scope"], item["resource_id"]): item for item in policies}


def _validated_policy_key(values, policies=None):
    scope = (values.get("scope") or "").strip()
    if scope not in db.ACCESS_SCOPES:
        raise ValueError("Invalid access policy scope.")
    try:
        resource_id = int(values.get("resource_id", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid access policy resource.") from exc
    policy = _policy_map(policies).get((scope, resource_id))
    if not policy:
        raise ValueError("Access policy resource not found.")
    return scope, resource_id, policy


def _decorate_requests(rows, policies):
    by_key = _policy_map(policies)
    result = []
    for row in rows:
        item = dict(row)
        policy = by_key.get((item["scope"], item["resource_id"]))
        item["gate_label"] = policy["label"] if policy else "Unavailable policy"
        item["scope_label"] = SCOPE_LABELS.get(item["scope"], "Policy")
        result.append(item)
    return result


def get_account_access_policies(user):
    """Build policy and request history rows for the account dashboard."""
    if not user or user["role"] == "admin":
        return []

    policies = _policies_with_labels()
    histories = {}
    for row in _decorate_requests(
        db.list_access_requests(user_id=user["id"], limit=200), policies
    ):
        histories.setdefault((row["scope"], row["resource_id"]), []).append(row)

    relevant_denials = set()
    for catalog_model in db.list_models(rolled_out_only=True):
        for surface in ("chat", "api"):
            for denial in model_access.get_denial_reasons(user, catalog_model, surface):
                if denial["scope"] in db.ACCESS_SCOPES:
                    relevant_denials.add((denial["scope"], denial["resource_id"]))

    result = []
    for policy in policies:
        key = (policy["scope"], policy["resource_id"])
        allowed = db.is_user_allowed_by_policy(*key, user["id"])
        is_capability = policy["scope"] in (
            "uncensored", "image_generation", "custom_personality",
        )
        relevant_denied = (
            not allowed
            and key in relevant_denials
            and (policy["persisted"] or bool(policy["requests_enabled"]))
        )
        if not is_capability and not relevant_denied and key not in histories:
            continue

        item = dict(policy)
        item["allowed"] = allowed
        item["history"] = histories.get(key, [])
        item["pending_request"] = next(
            (entry for entry in item["history"] if entry["status"] == "pending"),
            None,
        )
        item["can_request"] = (
            not allowed
            and bool(policy["requests_enabled"])
            and item["pending_request"] is None
        )
        result.append(item)
    return result


def _selected_redirect(scope, resource_id):
    return redirect(url_for(
        "admin_access_policies", scope=scope, resource_id=resource_id
    ))


def register_access_routes(app):

    @app.route("/admin/access", methods=["GET", "POST"])
    @admin_required
    def admin_access_policies():
        admin = get_current_user()
        policies = _policies_with_labels()

        if request.method == "POST":
            action = (request.form.get("action") or "").strip()
            if action not in ADMIN_ACTIONS:
                flash("Invalid access policy action.", "error")
                return redirect(url_for("admin_access_policies"))

            try:
                scope, resource_id, policy = _validated_policy_key(
                    request.form, policies
                )

                if action == "set_policy":
                    mode = (request.form.get("mode") or "").strip()
                    if mode not in db.ACCESS_MODES:
                        raise ValueError("Invalid access policy mode.")
                    raw_requests_enabled = request.form.get("requests_enabled")
                    if raw_requests_enabled not in (None, "1"):
                        raise ValueError("Invalid request setting.")
                    requests_enabled = raw_requests_enabled == "1"
                    db.set_access_policy(
                        scope, resource_id, mode, requests_enabled, admin["id"]
                    )
                    log_action(
                        "admin_set_access_policy", request, user=admin,
                        scope=scope, resource_id=resource_id, mode=mode,
                        requests_enabled=requests_enabled,
                    )
                    flash(f"Access policy for '{policy['label']}' updated.", "success")

                elif action == "add_membership":
                    list_type = (request.form.get("list_type") or "").strip()
                    if list_type not in db.ACCESS_LIST_TYPES:
                        raise ValueError("Invalid access list type.")
                    username = (request.form.get("username") or "").strip()
                    target = db.get_user_by_username(username) if username else None
                    if not target:
                        raise ValueError("User not found.")
                    reason = (request.form.get("reason") or "").strip()
                    if len(reason) > 1000:
                        raise ValueError("Reason must be 1,000 characters or fewer.")
                    expires_at = (request.form.get("expires_at") or "").strip() or None
                    if expires_at:
                        try:
                            parsed_expiry = datetime.fromisoformat(
                                expires_at.replace("Z", "+00:00")
                            )
                            if parsed_expiry.tzinfo is None:
                                parsed_expiry = parsed_expiry.replace(tzinfo=timezone.utc)
                            if parsed_expiry <= datetime.now(timezone.utc):
                                raise ValueError("Expiration date must be in the future.")
                            expires_at = parsed_expiry.astimezone(timezone.utc).isoformat()
                        except ValueError as exc:
                            raise ValueError("Invalid expiration date.") from exc
                    db.add_access_membership(
                        scope, resource_id, target["id"], list_type,
                        added_by=admin["id"], reason=reason, expires_at=expires_at,
                    )
                    log_action(
                        "admin_add_access_membership", request, user=admin,
                        scope=scope, resource_id=resource_id,
                        list_type=list_type, target_user_id=target["id"],
                    )
                    flash(
                        f"'{target['username']}' added to the {list_type}.",
                        "success",
                    )

                elif action == "remove_membership":
                    list_type = (request.form.get("list_type") or "").strip()
                    if list_type not in db.ACCESS_LIST_TYPES:
                        raise ValueError("Invalid access list type.")
                    target_user_id = (request.form.get("user_id") or "").strip()
                    target = db.get_user_by_id(target_user_id) if target_user_id else None
                    if not target:
                        raise ValueError("User not found.")
                    removed = db.remove_access_membership(
                        scope, resource_id, target["id"], list_type
                    )
                    if not removed:
                        raise ValueError("Access list entry was not found.")
                    log_action(
                        "admin_remove_access_membership", request, user=admin,
                        scope=scope, resource_id=resource_id,
                        list_type=list_type, target_user_id=target["id"],
                    )
                    flash(
                        f"'{target['username']}' removed from the {list_type}.",
                        "success",
                    )

                else:
                    decision = (request.form.get("decision") or "").strip()
                    if decision not in ("approve", "deny"):
                        raise ValueError("Invalid access request decision.")
                    try:
                        request_id = int(request.form.get("request_id", ""))
                    except (TypeError, ValueError) as exc:
                        raise ValueError("Invalid access request.") from exc
                    if request_id <= 0:
                        raise ValueError("Invalid access request.")
                    pending = db.get_access_request(request_id)
                    if not pending or pending["status"] != "pending":
                        raise ValueError(
                            "Access request was not found or was already resolved."
                        )
                    if (
                        pending["scope"] != scope
                        or pending["resource_id"] != resource_id
                    ):
                        raise ValueError("Access request does not match this policy.")
                    admin_message = (request.form.get("admin_message") or "").strip()
                    if len(admin_message) > 1000:
                        raise ValueError(
                            "Admin message must be 1,000 characters or fewer."
                        )
                    approved = decision == "approve"
                    db.resolve_access_request(
                        request_id, admin["id"], approved, admin_message or None
                    )
                    log_action(
                        "admin_approve_access_request" if approved
                        else "admin_deny_access_request",
                        request, user=admin, request_id=request_id,
                        scope=scope, resource_id=resource_id,
                        target_user_id=pending["user_id"],
                    )
                    flash(
                        "Access request approved." if approved
                        else "Access request denied.",
                        "success",
                    )
            except ValueError as exc:
                flash(str(exc), "error")
            return _selected_redirect(scope, resource_id) if "scope" in locals() \
                else redirect(url_for("admin_access_policies"))

        selected = policies[0]
        if "scope" in request.args or "resource_id" in request.args:
            try:
                _, _, selected = _validated_policy_key(request.args, policies)
            except ValueError as exc:
                flash(str(exc), "error")

        memberships = [
            dict(row) for row in db.list_access_memberships(
                selected["scope"], selected["resource_id"]
            )
        ]
        for membership in memberships:
            target = db.get_user_by_id(membership["user_id"])
            membership["target_role"] = target["role"] if target else None

        pending = _decorate_requests(
            db.list_access_requests(status="pending", limit=200), policies
        )
        resolved_rows = [
            *db.list_access_requests(status="approved", limit=100),
            *db.list_access_requests(status="denied", limit=100),
        ]
        resolved_rows.sort(
            key=lambda row: (row["resolved_at"] or row["created_at"], row["id"]),
            reverse=True,
        )
        resolved = _decorate_requests(resolved_rows[:200], policies)
        return render_template(
            "admin/access.html",
            capability_policies=[
                item for item in policies
                if item["scope"] in (
                    "uncensored", "image_generation", "custom_personality",
                )
            ],
            category_policies=[item for item in policies if item["scope"] == "category"],
            model_policies=[item for item in policies if item["scope"] == "model"],
            selected=selected,
            allowlist=[item for item in memberships if item["list_type"] == "allowlist"],
            denylist=[item for item in memberships if item["list_type"] == "denylist"],
            pending_requests=pending,
            resolved_requests=resolved,
            mode_labels=MODE_LABELS,
        )

    @app.route("/account/access-request", methods=["POST"])
    @login_required
    def account_access_request():
        user = get_current_user()
        if user["role"] == "admin":
            flash("Admin accounts already bypass access policies.", "info")
            return redirect(url_for("account"))

        try:
            policies = get_account_access_policies(user)
            scope, resource_id, policy = _validated_policy_key(
                request.form, policies
            )
            visible_policy = _policy_map(policies).get((scope, resource_id))
            if not visible_policy or visible_policy["allowed"]:
                raise ValueError("You already have access to this policy.")
            if not visible_policy["requests_enabled"]:
                raise ValueError("Access requests are disabled for this policy.")
            if visible_policy["pending_request"]:
                raise ValueError("A pending request already exists for this policy.")

            use_case = (request.form.get("use_case") or "").strip()
            if not use_case:
                raise ValueError("A use case is required.")
            if len(use_case) > 2000:
                raise ValueError("Use case must be 2,000 characters or fewer.")
            confirmed_safe = request.form.get("confirmed_safe") == "1"
            confirmed_logging = request.form.get("confirmed_logging") == "1"
            if not confirmed_safe or not confirmed_logging:
                raise ValueError("Both confirmations are required.")

            request_id = db.create_access_request(
                scope, resource_id, user["id"], use_case,
                confirmed_safe, confirmed_logging,
            )
            log_action(
                "submit_access_request", request, user=user,
                request_id=request_id, scope=scope, resource_id=resource_id,
            )
            flash(f"Access request for '{policy['label']}' submitted.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("account"))
