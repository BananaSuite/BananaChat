"""Access policies with per-user allow/deny lists and access requests.

A policy guards a *scope*: the capabilities ``uncensored``,
``image_generation``, ``custom_personality`` and ``agents`` (resource id 0), or one
``category``/``model`` (its id). Modes:

* ``allow_all`` - everyone passes;
* ``deny_except_allowlist`` - only unexpired allowlist entries pass;
* ``allow_except_denylist`` - everyone except unexpired denylist entries.

Scopes without a stored row use the defaults below.
"""

from __future__ import annotations

from bananachat import db

SCOPES = ("uncensored", "image_generation", "custom_personality", "agents", "category", "model")
CAPABILITY_SCOPES = ("uncensored", "image_generation", "custom_personality", "agents")
MODES = ("allow_all", "deny_except_allowlist", "allow_except_denylist")
LIST_TYPES = ("allowlist", "denylist")
DEFAULTS = {
    "uncensored": ("deny_except_allowlist", 1),
    "image_generation": ("allow_all", 1),
    "custom_personality": ("allow_except_denylist", 1),
    # Coding agents run commands in cloud sandboxes: only people an administrator allows.
    "agents": ("deny_except_allowlist", 1),
    "category": ("allow_all", 0),
    "model": ("allow_all", 0),
}
SCOPE_LABELS = {
    "uncensored": "Uncensored models",
    "image_generation": "Image generation",
    "custom_personality": "Custom personalities",
    "agents": "Agents",
    "category": "Category",
    "model": "Model",
}


def check_key(scope: str, resource_id) -> int:
    if scope not in SCOPES:
        raise ValueError("Unknown access scope.")
    try:
        resource_id = int(resource_id)
    except (TypeError, ValueError):
        raise ValueError("Invalid resource.") from None
    if scope in CAPABILITY_SCOPES and resource_id != 0:
        raise ValueError("Capability policies have no resource id.")
    if scope in ("category", "model"):
        table = "model_categories" if scope == "category" else "ai_models"
        if resource_id <= 0 or not db.one(f"SELECT 1 FROM {table} WHERE id=?", (resource_id,)):
            raise ValueError("That model or category no longer exists.")
    return resource_id


def default_policy(scope: str, resource_id: int) -> dict:
    mode, requests_enabled = DEFAULTS[scope]
    return {"scope": scope, "resource_id": resource_id, "mode": mode, "requests_enabled": requests_enabled,
            "updated_by": None, "updated_at": None, "persisted": False}


def get_policy(scope: str, resource_id: int = 0) -> dict:
    row = db.one("SELECT * FROM model_access_policies WHERE scope=? AND resource_id=?", (scope, resource_id))
    if row is None:
        return default_policy(scope, resource_id)
    return {**row.to_dict(), "persisted": True}


def all_policies() -> dict[tuple[str, int], dict]:
    return {(row["scope"], row["resource_id"]): {**row.to_dict(), "persisted": True}
            for row in db.query("SELECT * FROM model_access_policies")}


def _ensure(scope: str, resource_id: int) -> None:
    mode, requests_enabled = DEFAULTS[scope]
    db.execute("INSERT OR IGNORE INTO model_access_policies (scope, resource_id, mode, requests_enabled, "
               "created_at, updated_at) VALUES (?,?,?,?,?,?)",
               (scope, resource_id, mode, requests_enabled, db.now(), db.now()))


def set_policy(scope: str, resource_id: int, mode: str, requests_enabled: bool, updated_by: str | None) -> None:
    if mode not in MODES:
        raise ValueError("Unknown access mode.")
    with db.transaction():
        resource_id = check_key(scope, resource_id)
        db.execute(
            "INSERT INTO model_access_policies (scope, resource_id, mode, requests_enabled, updated_by, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(scope, resource_id) DO UPDATE SET mode=excluded.mode, "
            "requests_enabled=excluded.requests_enabled, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (scope, resource_id, mode, 1 if requests_enabled else 0, updated_by, db.now(), db.now()))


def user_memberships(user_id: str) -> dict[tuple[str, int], set[str]]:
    """Unexpired list memberships of one user: ``{(scope, id): {"allowlist", ...}}``."""
    result: dict[tuple[str, int], set[str]] = {}
    for row in db.query("SELECT scope, resource_id, list_type FROM model_access_memberships WHERE user_id=? "
                        "AND (expires_at IS NULL OR expires_at>?)", (user_id, db.now())):
        result.setdefault((row["scope"], row["resource_id"]), set()).add(row["list_type"])
    return result


def is_allowed(policy: dict, lists: set[str]) -> bool:
    mode = policy["mode"]
    if mode == "deny_except_allowlist":
        return "allowlist" in lists
    if mode == "allow_except_denylist":
        return "denylist" not in lists
    return True


def add_membership(scope: str, resource_id: int, user_id: str, list_type: str, *, added_by: str | None,
                   reason: str = "", expires_at: str | None = None) -> None:
    if list_type not in LIST_TYPES:
        raise ValueError("Unknown list.")
    with db.transaction():
        resource_id = check_key(scope, resource_id)
        _ensure(scope, resource_id)
        db.execute(
            "INSERT INTO model_access_memberships (scope, resource_id, user_id, list_type, added_by, reason, expires_at, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(scope, resource_id, user_id, list_type) "
            "DO UPDATE SET added_by=excluded.added_by, reason=excluded.reason, expires_at=excluded.expires_at, "
            "updated_at=excluded.updated_at",
            (scope, resource_id, user_id, list_type, added_by, (reason or "")[:500], expires_at, db.now(), db.now()))


def remove_membership(scope: str, resource_id: int, user_id: str, list_type: str) -> None:
    db.execute("DELETE FROM model_access_memberships WHERE scope=? AND resource_id=? AND user_id=? AND list_type=?",
               (scope, resource_id, user_id, list_type))


def list_memberships(scope: str, resource_id: int):
    return db.query("SELECT m.*, u.username, a.username AS added_by_name FROM model_access_memberships m "
                    "JOIN users u ON u.id=m.user_id LEFT JOIN users a ON a.id=m.added_by "
                    "WHERE m.scope=? AND m.resource_id=? ORDER BY m.list_type, u.username COLLATE NOCASE",
                    (scope, resource_id))


def membership_counts() -> dict[tuple[str, int], dict[str, int]]:
    result: dict[tuple[str, int], dict[str, int]] = {}
    for row in db.query("SELECT scope, resource_id, list_type, COUNT(*) AS n FROM model_access_memberships "
                        "WHERE expires_at IS NULL OR expires_at>? GROUP BY scope, resource_id, list_type", (db.now(),)):
        result.setdefault((row["scope"], row["resource_id"]), {})[row["list_type"]] = row["n"]
    return result


# ----- requests -------------------------------------------------------------

def create_request(scope: str, resource_id: int, user_id: str, use_case: str, *,
                   confirmed_safe: bool, confirmed_logging: bool) -> int:
    if not (confirmed_safe and confirmed_logging):
        raise ValueError("Both confirmations are required.")
    use_case = (use_case or "").strip()
    if not 10 <= len(use_case) <= 2000:
        raise ValueError("Describe your use case in 10-2000 characters.")
    with db.transaction():
        resource_id = check_key(scope, resource_id)
        policy = get_policy(scope, resource_id)
        if not policy["requests_enabled"]:
            raise ValueError("Requests are not accepted for this resource.")
        if db.one("SELECT 1 FROM model_access_requests WHERE scope=? AND resource_id=? AND user_id=? AND "
                  "status='pending'", (scope, resource_id, user_id)):
            raise ValueError("You already have a pending request for this resource.")
        cursor = db.execute(
            "INSERT INTO model_access_requests (scope, resource_id, user_id, use_case, confirmed_safe, "
            "confirmed_logging, status, created_at) VALUES (?,?,?,?,1,1,'pending',?)",
            (scope, resource_id, user_id, use_case, db.now()))
        return cursor.lastrowid


def pending_requests_for(user_id: str) -> set[tuple[str, int]]:
    return {(row["scope"], row["resource_id"]) for row in db.query(
        "SELECT scope, resource_id FROM model_access_requests WHERE user_id=? AND status='pending'", (user_id,))}


def list_requests(*, status: str | None = "pending", user_id: str | None = None, limit: int = 200):
    clauses, params = [], []
    if status:
        clauses.append("r.status=?")
        params.append(status)
    if user_id:
        clauses.append("r.user_id=?")
        params.append(user_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return db.query(
        "SELECT r.*, u.username, a.username AS resolved_by_name, "
        "CASE r.scope WHEN 'model' THEN (SELECT display_name FROM ai_models WHERE id=r.resource_id) "
        "WHEN 'category' THEN (SELECT name FROM model_categories WHERE id=r.resource_id) END AS resource_name "
        f"FROM model_access_requests r JOIN users u ON u.id=r.user_id LEFT JOIN users a ON a.id=r.resolved_by {where} "
        "ORDER BY r.created_at DESC LIMIT ?", (*params, limit))


def resolve_request(request_id: int, resolved_by: str, approved: bool, message: str = "") -> None:
    with db.transaction():
        request = db.one("SELECT * FROM model_access_requests WHERE id=?", (request_id,))
        if request is None or request["status"] != "pending":
            raise ValueError("This request was already resolved.")
        db.execute("UPDATE model_access_requests SET status=?, admin_message=?, resolved_by=?, resolved_at=? WHERE id=?",
                   ("approved" if approved else "denied", (message or "")[:1000] or None, resolved_by, db.now(),
                    request_id))
        if approved:
            scope, resource_id, user_id = request["scope"], request["resource_id"], request["user_id"]
            policy = get_policy(scope, resource_id)
            if policy["mode"] == "allow_except_denylist":
                remove_membership(scope, resource_id, user_id, "denylist")
            else:
                # Approval grants a permanent entry, even if an expired one existed.
                add_membership(scope, resource_id, user_id, "allowlist", added_by=resolved_by,
                               reason="Approved access request", expires_at=None)
