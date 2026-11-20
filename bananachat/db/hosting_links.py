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
