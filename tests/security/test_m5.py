"""Regression tests for the core, administration and account findings of the full re-audit (AUDIT.md, MC1-*)."""

from __future__ import annotations

import re

from tests.app.fixtures import Browser
from tests.app.test_chat import new_chat, user_browser

MiB = 1024 * 1024


def test_mc1_1_no_history_audit_page_stops_at_a_size(make_app, make_user):
    from bananachat.db import chats, users

    app = make_app()
    alice = user_browser(app, make_user)
    session_id = new_chat(alice, incognito=True)
    with app.app_context():
        user_id = users.get_by_username("alice")["id"]
        for n in range(12):  # 12 copies of 600 KB '&' (7.2 MB, 36 MB once escaped)
            chats.add_message(session_id, "user", f"entry-{n:02d}:" + "&" * 600_000, user_id=user_id,
                              incognito=True)
    assert alice.post(f"/chat/{session_id}/delete").status_code == 302
    admin = Browser(app)
    admin.login("admin", "admin-password")

    seen, url, pages = [], f"/admin/incognito/{session_id}", 0
    while url:
        page = admin.get(url)
        assert page.status_code == 200
        html = page.get_data(as_text=True)
        assert len(page.data) < 6 * 4 * MiB  # 4 MiB of stored text, escaped
        entries = re.findall(r"entry-(\d\d):", html)
        assert entries and len(entries) < 12
        seen = entries + seen
        earlier = re.search(r'href="([^"]*before=\d+)"', html)
        url = earlier.group(1).replace("&amp;", "&") if earlier else None
        pages += 1
    # Every entry stays reachable through "Show earlier entries", once, in order.
    assert pages >= 2 and seen == [f"{n:02d}" for n in range(12)]
    assert admin.get(f"/admin/incognito/{session_id}?before=x").status_code == 400


def test_mc1_2_a_refused_database_export_is_neither_copied_nor_audited(make_app, monkeypatch):
    import sqlite_snapshot

    from bananachat import db
    from tests.security.test_reaudit import _held, _image_chat

    app = make_app(download_threads=8)  # 2 download places per client
    admin = Browser(app)
    admin.login("admin", "admin-password")  # a fresh sign-in counts as confirmed
    session_id, attachment = _image_chat(app, admin, "admin")
    held = [_held(admin, attachment), _held(admin, f"/chat/{session_id}/download")]
    snapshots = []
    real = sqlite_snapshot.snapshot
    monkeypatch.setattr(sqlite_snapshot, "snapshot", lambda *a, **k: snapshots.append(a) or real(*a, **k))

    def exports():
        with app.app_context():
            return db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='admin.migration_export'")

    for _ in range(3):
        refused = admin.post("/admin/migration/export")
        assert refused.status_code == 429 and refused.headers["Retry-After"]
        refused.close()
    assert snapshots == [] and exports() == 0
    assert not list(app.config["BC"].instance_dir.glob(".export-*"))
    for response in held:
        response.close()
    # With a place free the export runs, once, and gives the place back when it is read.
    exported = admin.post("/admin/migration/export")
    assert exported.status_code == 200 and exported.mimetype == "application/gzip"
    exported.get_data()
    exported.close()
    assert len(snapshots) == 1 and exports() == 1
    assert app.extensions["bananachat.downloads"].active == 0


def _audit_rows(app, action):
    from bananachat import db

    with app.app_context():
        return db.scalar("SELECT COUNT(*) FROM audit_log WHERE action=?", (action,))


def test_mc1_3_sign_out_everywhere_else_always_works_and_stops_adding_audit_entries(app, make_user, browser_for,
                                                                                    monkeypatch):
    from bananachat import db
    from bananachat.db import tokens
    from bananachat.web import account

    monkeypatch.setattr(account, "SESSIONS_REVOKE_AUDITS", 3)
    user = make_user("mallory")
    thief, owner = browser_for(), browser_for()
    thief.login("mallory")
    owner.login("mallory")
    # Someone holding a session in the account repeats it: it is never refused, the audit rows stop at the budget.
    codes = [thief.post("/account/sessions/revoke-others", {}).status_code for _ in range(12)]
    assert codes == [302] * 12
    assert _audit_rows(app, "account.sessions_revoke_others") == 3
    # The owner, signed out by it, signs in again and still ends the thief's session and API tokens.
    assert owner.get("/account").status_code == 302
    owner.login("mallory")
    with app.app_context():
        tokens.create(user["id"], "thief's")
    assert owner.post("/account/sessions/revoke-others", {"revoke_api_tokens": "1"}).status_code == 302
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM auth_sessions WHERE user_id=?", (user["id"],)) == 1
        assert tokens.count_active(user["id"]) == 0
    assert thief.get("/account").status_code == 302
    assert owner.get("/account").status_code == 200
    assert _audit_rows(app, "account.sessions_revoke_others") == 3
    assert _audit_rows(app, "account.api_tokens_revoke_all") == 1
    # The budget is the account's own: another account's sign-outs are still recorded.
    make_user("bob")
    bob = browser_for()
    bob.login("bob")
    assert bob.post("/account/sessions/revoke-others", {}).status_code == 302
    assert _audit_rows(app, "account.sessions_revoke_others") == 4


def test_mc1_3_unsharing_a_personality_that_is_not_shared_writes_no_audit_entry(app, make_user, browser_for):
    from bananachat import db

    user = make_user("eve")
    eve = browser_for()
    eve.login("eve")
    assert eve.post("/personalities", {"name": "p1", "instructions": "be nice"}).status_code == 302
    with app.app_context():
        personality_id = db.scalar("SELECT id FROM personalities WHERE user_id=?", (user["id"],))
    for _ in range(50):
        assert eve.post(f"/personalities/{personality_id}/unshare").status_code == 302
    assert _audit_rows(app, "personality.unshare") == 0
    # A real unshare is still recorded, once.
    assert eve.post(f"/personalities/{personality_id}/share").status_code == 302
    eve.post(f"/personalities/{personality_id}/unshare")
    eve.post(f"/personalities/{personality_id}/unshare")
    assert _audit_rows(app, "personality.unshare") == 1
    with app.app_context():
        assert db.scalar("SELECT share_token FROM personalities WHERE id=?", (personality_id,)) is None


def test_mc1_3_signing_devices_out_one_by_one_stops_adding_audit_entries(app, make_user, browser_for, monkeypatch):
    from bananachat import db
    from bananachat.web import account

    monkeypatch.setattr(account, "SESSION_REVOKE_AUDITS", 3)
    user = make_user("mallory")
    owner = browser_for()
    owner.login("mallory")
    other = browser_for()
    for _ in range(6):  # sign in elsewhere, then sign that device out from the first one
        assert other.post("/login", {"username": "mallory", "password": "mallory-password"}).status_code == 302
        with app.app_context():
            newest = db.scalar("SELECT id_hash FROM auth_sessions WHERE user_id=? ORDER BY rowid DESC", (user["id"],))
        assert owner.post(f"/account/sessions/{newest}/revoke").status_code == 302
        # The session always ends, past the limit too: the owner can always sign a stranger out.
        with app.app_context():
            assert db.scalar("SELECT COUNT(*) FROM auth_sessions WHERE id_hash=?", (newest,)) == 0
        assert other.get("/account").status_code == 302
    assert _audit_rows(app, "account.session_revoke") == 3
    # The budget is the account's own: another account's sign-outs are still recorded.
    bob = make_user("bob")
    first, second = browser_for(), browser_for()
    first.login("bob")
    second.login("bob")
    with app.app_context():
        newest = db.scalar("SELECT id_hash FROM auth_sessions WHERE user_id=? ORDER BY rowid DESC", (bob["id"],))
    assert first.post(f"/account/sessions/{newest}/revoke").status_code == 302
    assert _audit_rows(app, "account.session_revoke") == 4
