"""Links between accounts and BananaWiki Hosting accounts (``hosting_links``, schema version 30).

A link is keyed on the portal's stable account id (``sub``), never on a user
name or an email. An account has at most one link and a portal account links
at most one account (both columns are unique).
"""

from __future__ import annotations

from bananachat import db


def link_of(user_id: str):
    """The link of account *user_id*, or None."""
    return db.one("SELECT * FROM hosting_links WHERE user_id=?", (user_id,)) if user_id else None


def linked_user(account_id: str):
    """The account linked to portal account *account_id*, or None."""
    if not account_id:
        return None
    return db.one("SELECT u.* FROM hosting_links l JOIN users u ON u.id=l.user_id WHERE l.account_id=?",
                  (account_id,))


def create(user_id: str, account_id: str, portal_username: str) -> None:
    """Link *user_id* to *account_id*; raises ``sqlite3.IntegrityError`` when either is already linked."""
    db.execute("INSERT INTO hosting_links (user_id, account_id, portal_username, linked_at) VALUES (?,?,?,?)",
               (user_id, account_id, portal_username[:200], db.now()))


def remove(user_id: str):
    """Remove the link of *user_id*; returns the removed link, or None when there was none."""
    with db.transaction():
        row = link_of(user_id)
        if row is not None:
            db.execute("DELETE FROM hosting_links WHERE user_id=?", (user_id,))
        return row


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def users_page(limit: int, offset: int, search: str = "", state: str = ""):
    """Accounts with their link (``account_id``, ``portal_username``, ``linked_at``; NULL when not linked).

    *state* narrows them: ``linked`` or ``unlinked``.
    """
    where, params = _filters(search, state)
    return db.query("SELECT u.id, u.username, u.role, u.suspended, u.suspended_until, u.password_set, "
                    "l.account_id, l.portal_username, l.linked_at FROM users u "
                    f"LEFT JOIN hosting_links l ON l.user_id=u.id {where} "
                    "ORDER BY lower(u.username), u.id LIMIT ? OFFSET ?", (*params, limit, offset))


def users_count(search: str = "", state: str = "") -> int:
    where, params = _filters(search, state)
    return db.scalar(f"SELECT COUNT(*) FROM users u LEFT JOIN hosting_links l ON l.user_id=u.id {where}",
                     tuple(params), 0)


def _filters(search: str, state: str):
    clauses, params = [], []
    if search:
        clauses.append("u.username LIKE ? ESCAPE '\\'")
        params.append("%" + _escape_like(search) + "%")
    if state == "linked":
        clauses.append("l.user_id IS NOT NULL")
    elif state == "unlinked":
        clauses.append("l.user_id IS NULL")
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def counts() -> dict:
    """``{"users", "linked", "admins_unlinked"}``: accounts, linked ones, active administrators not linked."""
    row = db.one("SELECT COUNT(*) AS users, COUNT(l.user_id) AS linked, "
                 "SUM(CASE WHEN u.role='admin' AND u.suspended=0 AND l.user_id IS NULL THEN 1 ELSE 0 END) "
                 "AS admins_unlinked FROM users u LEFT JOIN hosting_links l ON l.user_id=u.id")
    return {"users": row["users"] or 0, "linked": row["linked"] or 0, "admins_unlinked": row["admins_unlinked"] or 0}


def unlinked_admins() -> list[str]:
    """User names of the active administrators without a link (hosting-only sign-in would lock them out)."""
    return [row["username"] for row in db.query(
        "SELECT u.username FROM users u WHERE u.role='admin' AND u.suspended=0 "
        "AND NOT EXISTS (SELECT 1 FROM hosting_links l WHERE l.user_id=u.id) ORDER BY lower(u.username)")]
