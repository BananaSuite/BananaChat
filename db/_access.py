"""Persistent model access policies, memberships, and requests."""

import sqlite3
from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


ACCESS_SCOPES = (
    "uncensored", "image_generation", "custom_personality", "category", "model",
)
ACCESS_MODES = ("allow_all", "deny_except_allowlist", "allow_except_denylist")
ACCESS_LIST_TYPES = ("allowlist", "denylist")

_DEFAULTS = {
    "uncensored": ("deny_except_allowlist", 1),
    "image_generation": ("allow_all", 1),
    "custom_personality": ("allow_except_denylist", 1),
    "category": ("allow_all", 0),
    "model": ("allow_all", 0),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate_key(scope, resource_id):
    if scope not in ACCESS_SCOPES:
        raise ValueError(f"Invalid access scope: {scope}")
    try:
        resource_id = int(resource_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("resource_id must be an integer") from exc
    if scope in ("uncensored", "image_generation", "custom_personality") and resource_id != 0:
        raise ValueError(f"{scope} policies must use resource_id=0")
    if scope in ("category", "model") and resource_id <= 0:
        raise ValueError(f"{scope} policies require a positive resource_id")
    return resource_id


def _validate_resource(conn, scope, resource_id):
    if scope == "category":
        exists = conn.execute(
            "SELECT 1 FROM model_categories WHERE id=?", (resource_id,)
        ).fetchone()
    elif scope == "model":
        exists = conn.execute(
            "SELECT 1 FROM ai_models WHERE id=?", (resource_id,)
        ).fetchone()
    else:
        exists = True
    if not exists:
        raise ValueError(f"Unknown {scope} resource: {resource_id}")


def _policy_from_conn(conn, scope, resource_id):
    row = conn.execute(
        "SELECT * FROM model_access_policies WHERE scope=? AND resource_id=?",
        (scope, resource_id),
    ).fetchone()
    if row:
        result = dict(row)
        result["persisted"] = True
        return result
    mode, requests_enabled = _DEFAULTS[scope]
    return {
        "scope": scope,
        "resource_id": resource_id,
        "mode": mode,
        "requests_enabled": requests_enabled,
        "updated_by": None,
        "created_at": None,
        "updated_at": None,
        "persisted": False,
    }


def _ensure_policy(conn, scope, resource_id):
    mode, requests_enabled = _DEFAULTS[scope]
    conn.execute(
        "INSERT OR IGNORE INTO model_access_policies "
        "(scope, resource_id, mode, requests_enabled) VALUES (?,?,?,?)",
        (scope, resource_id, mode, requests_enabled),
    )


@retry_on_busy
def get_access_policy(scope, resource_id=0):
    """Return a persisted policy or the virtual default for its scope."""
    resource_id = _validate_key(scope, resource_id)
    with get_db_context() as conn:
        return _policy_from_conn(conn, scope, resource_id)


def set_access_policy(scope, resource_id=0, mode="allow_all",
                      requests_enabled=False, updated_by=None):
    """Create or update a policy without changing either membership list."""
    resource_id = _validate_key(scope, resource_id)
    if mode not in ACCESS_MODES:
        raise ValueError(f"Invalid access mode: {mode}")
    now = _now()
    with get_db_context() as conn:
        _validate_resource(conn, scope, resource_id)
        conn.execute(
            "INSERT INTO model_access_policies "
            "(scope, resource_id, mode, requests_enabled, updated_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(scope, resource_id) DO UPDATE SET "
            "mode=excluded.mode, requests_enabled=excluded.requests_enabled, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (scope, resource_id, mode, int(bool(requests_enabled)), updated_by, now, now),
        )
        conn.commit()
        return _policy_from_conn(conn, scope, resource_id)


@retry_on_busy
def list_access_policies(scope=None, surface=None):
    """List policy-aware resources with labels and membership/request counts.

    Category resources are restricted to the requested ``chat`` or ``api``
    surface. Models and categories without a stored policy are represented by
    their virtual allow-all, requests-off default.
    """
    if scope is not None and scope not in ACCESS_SCOPES:
        raise ValueError(f"Invalid access scope: {scope}")
    if surface not in (None, "chat", "api"):
        raise ValueError("surface must be 'chat', 'api', or None")

    with get_db_context() as conn:
        resources = []
        if scope in (None, "uncensored"):
            resources.append(("uncensored", 0, "Uncensored models"))
        if scope in (None, "image_generation"):
            resources.append(("image_generation", 0, "Image generation"))
        if scope in (None, "custom_personality"):
            resources.append(("custom_personality", 0, "Custom personalities"))
        if scope in (None, "category"):
            sql = "SELECT id, name FROM model_categories"
            params = ()
            if surface:
                sql += " WHERE scope IN (?, 'both')"
                params = (surface,)
            sql += " ORDER BY sort_order ASC, name ASC"
            resources.extend(
                ("category", row["id"], row["name"])
                for row in conn.execute(sql, params).fetchall()
            )
        if scope in (None, "model"):
            resources.extend(
                ("model", row["id"], row["display_name"])
                for row in conn.execute(
                    "SELECT id, display_name FROM ai_models "
                    "ORDER BY sort_order ASC, display_name ASC"
                ).fetchall()
            )

        membership_counts = {
            (row["scope"], row["resource_id"], row["list_type"]): row["count"]
            for row in conn.execute(
                "SELECT scope, resource_id, list_type, COUNT(*) AS count "
                "FROM model_access_memberships GROUP BY scope, resource_id, list_type"
            ).fetchall()
        }
        pending_counts = {
            (row["scope"], row["resource_id"]): row["count"]
            for row in conn.execute(
                "SELECT scope, resource_id, COUNT(*) AS count "
                "FROM model_access_requests WHERE status='pending' "
                "GROUP BY scope, resource_id"
            ).fetchall()
        }

        result = []
        for resource_scope, resource_id, label in resources:
            policy = _policy_from_conn(conn, resource_scope, resource_id)
            policy.update({
                "label": label,
                "allowlist_count": membership_counts.get(
                    (resource_scope, resource_id, "allowlist"), 0
                ),
                "denylist_count": membership_counts.get(
                    (resource_scope, resource_id, "denylist"), 0
                ),
                "pending_request_count": pending_counts.get(
                    (resource_scope, resource_id), 0
                ),
            })
            result.append(policy)
        return result


def add_access_membership(scope, resource_id, user_id, list_type,
                          added_by=None, reason="", expires_at=None):
    """Add or update one allowlist/denylist entry, preserving the other list."""
    resource_id = _validate_key(scope, resource_id)
    if list_type not in ACCESS_LIST_TYPES:
        raise ValueError(f"Invalid access list type: {list_type}")
    now = _now()
    with get_db_context() as conn:
        _validate_resource(conn, scope, resource_id)
        _ensure_policy(conn, scope, resource_id)
        conn.execute(
            "INSERT INTO model_access_memberships "
            "(scope, resource_id, user_id, list_type, added_by, reason, expires_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope, resource_id, user_id, list_type) DO UPDATE SET "
            "added_by=excluded.added_by, reason=excluded.reason, "
            "expires_at=excluded.expires_at, updated_at=excluded.updated_at",
            (
                scope, resource_id, user_id, list_type, added_by, reason or "",
                expires_at, now, now,
            ),
        )
        conn.commit()
        return conn.execute(
            "SELECT * FROM model_access_memberships "
            "WHERE scope=? AND resource_id=? AND user_id=? AND list_type=?",
            (scope, resource_id, user_id, list_type),
        ).fetchone()


def remove_access_membership(scope, resource_id, user_id, list_type):
    resource_id = _validate_key(scope, resource_id)
    if list_type not in ACCESS_LIST_TYPES:
        raise ValueError(f"Invalid access list type: {list_type}")
    with get_db_context() as conn:
        cur = conn.execute(
            "DELETE FROM model_access_memberships "
            "WHERE scope=? AND resource_id=? AND user_id=? AND list_type=?",
            (scope, resource_id, user_id, list_type),
        )
        conn.commit()
        return cur.rowcount > 0


@retry_on_busy
def list_access_memberships(scope=None, resource_id=None, list_type=None):
    if scope is not None:
        if resource_id is None:
            raise ValueError("resource_id is required when scope is provided")
        resource_id = _validate_key(scope, resource_id)
    elif resource_id is not None:
        raise ValueError("scope is required when resource_id is provided")
    if list_type is not None and list_type not in ACCESS_LIST_TYPES:
        raise ValueError(f"Invalid access list type: {list_type}")

    clauses = []
    params = []
    if scope is not None:
        clauses.extend(("mam.scope=?", "mam.resource_id=?"))
        params.extend((scope, resource_id))
    if list_type is not None:
        clauses.append("mam.list_type=?")
        params.append(list_type)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT mam.*, u.username, admin.username AS added_by_username "
            "FROM model_access_memberships mam "
            "JOIN users u ON u.id=mam.user_id "
            "LEFT JOIN users admin ON admin.id=mam.added_by" + where +
            " ORDER BY mam.created_at DESC, mam.id DESC",
            params,
        ).fetchall()


@retry_on_busy
def is_user_allowed_by_policy(scope, resource_id, user_id):
    """Evaluate one policy gate using its mode and the relevant list only."""
    resource_id = _validate_key(scope, resource_id)
    with get_db_context() as conn:
        policy = _policy_from_conn(conn, scope, resource_id)
        if policy["mode"] == "allow_all":
            return True
        list_type = (
            "allowlist" if policy["mode"] == "deny_except_allowlist" else "denylist"
        )
        present = conn.execute(
            "SELECT 1 FROM model_access_memberships "
            "WHERE scope=? AND resource_id=? AND user_id=? AND list_type=? "
            "AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))",
            (scope, resource_id, user_id, list_type),
        ).fetchone() is not None
        if policy["mode"] == "deny_except_allowlist":
            return present
        return not present


def create_access_request(scope, resource_id, user_id, use_case,
                          confirmed_safe, confirmed_logging):
    """Create a pending request; only one may exist for a user and gate."""
    resource_id = _validate_key(scope, resource_id)
    if not confirmed_safe or not confirmed_logging:
        raise ValueError("Both safety and logging confirmations are required")
    with get_db_context() as conn:
        _validate_resource(conn, scope, resource_id)
        policy = _policy_from_conn(conn, scope, resource_id)
        if not policy["requests_enabled"]:
            raise ValueError("Access requests are disabled for this policy")
        _ensure_policy(conn, scope, resource_id)
        try:
            cur = conn.execute(
                "INSERT INTO model_access_requests "
                "(scope, resource_id, user_id, use_case, confirmed_safe, confirmed_logging) "
                "VALUES (?,?,?,?,1,1)",
                (scope, resource_id, user_id, (use_case or "").strip()),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if "unique" in str(exc).lower():
                raise ValueError("A pending request already exists for this policy") from exc
            raise
        return cur.lastrowid


@retry_on_busy
def get_access_request(request_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT mar.*, u.username, resolver.username AS resolved_by_username "
            "FROM model_access_requests mar "
            "JOIN users u ON u.id=mar.user_id "
            "LEFT JOIN users resolver ON resolver.id=mar.resolved_by "
            "WHERE mar.id=?",
            (request_id,),
        ).fetchone()


@retry_on_busy
def get_pending_access_request(scope, resource_id, user_id):
    resource_id = _validate_key(scope, resource_id)
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM model_access_requests "
            "WHERE scope=? AND resource_id=? AND user_id=? AND status='pending'",
            (scope, resource_id, user_id),
        ).fetchone()


@retry_on_busy
def list_access_requests(status=None, user_id=None, scope=None,
                         resource_id=None, limit=200, offset=0):
    if status not in (None, "pending", "approved", "denied"):
        raise ValueError(f"Invalid request status: {status}")
    if scope is not None:
        if resource_id is not None:
            resource_id = _validate_key(scope, resource_id)
        elif scope not in ACCESS_SCOPES:
            raise ValueError(f"Invalid access scope: {scope}")
    elif resource_id is not None:
        raise ValueError("scope is required when resource_id is provided")

    clauses = []
    params = []
    for column, value in (
        ("mar.status", status), ("mar.user_id", user_id), ("mar.scope", scope),
        ("mar.resource_id", resource_id),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.extend((int(limit), int(offset)))
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT mar.*, u.username, resolver.username AS resolved_by_username "
            "FROM model_access_requests mar "
            "JOIN users u ON u.id=mar.user_id "
            "LEFT JOIN users resolver ON resolver.id=mar.resolved_by" + where +
            " ORDER BY mar.created_at DESC, mar.id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return rows


def resolve_access_request(request_id, resolved_by, approved, admin_message=None):
    """Resolve a request and atomically apply approval membership changes."""
    now = _now()
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        request_row = conn.execute(
            "SELECT * FROM model_access_requests WHERE id=? AND status='pending'",
            (request_id,),
        ).fetchone()
        if not request_row:
            conn.rollback()
            raise ValueError("Access request was not found or was already resolved")

        status = "approved" if approved else "denied"
        if approved:
            policy = _policy_from_conn(
                conn, request_row["scope"], request_row["resource_id"]
            )
            reason = f"Approved access request #{request_id}"
            conn.execute(
                "INSERT INTO model_access_memberships "
                "(scope, resource_id, user_id, list_type, added_by, reason, created_at, updated_at) "
                "VALUES (?,?,?,'allowlist',?,?,?,?) "
                "ON CONFLICT(scope, resource_id, user_id, list_type) DO UPDATE SET "
                "added_by=excluded.added_by, reason=excluded.reason, updated_at=excluded.updated_at",
                (
                    request_row["scope"], request_row["resource_id"],
                    request_row["user_id"], resolved_by, reason, now, now,
                ),
            )
            if policy["mode"] == "allow_except_denylist":
                conn.execute(
                    "DELETE FROM model_access_memberships "
                    "WHERE scope=? AND resource_id=? AND user_id=? AND list_type='denylist'",
                    (
                        request_row["scope"], request_row["resource_id"],
                        request_row["user_id"],
                    ),
                )

        cur = conn.execute(
            "UPDATE model_access_requests SET status=?, admin_message=?, "
            "resolved_by=?, resolved_at=? WHERE id=? AND status='pending'",
            (status, admin_message, resolved_by, now, request_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise ValueError("Access request was already resolved")
        conn.commit()
    return get_access_request(request_id)
