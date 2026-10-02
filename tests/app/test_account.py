"""Account page: usage, password and sessions, exports, deletion, quota and access requests."""

from __future__ import annotations

import json

import pytest

from tests.app.conftest import TEST_CSRF, Browser


def _signed_in(app, username, password=None):
    """A signed-in browser whose CSRF token stays fixed even after it renders pages."""
    browser = Browser(app)
    browser.login(username, password)
    with browser.client.session_transaction() as session:
        session["csrf"] = TEST_CSRF
    return browser


def test_account_page_shows_budgets_including_the_music_bonus(app, make_user):
    from bananachat.db import credits, limits, settings

    user = make_user("anna")
    with app.app_context():
        settings.update(music_enabled=1, music_bonus_mode="multiplier", music_credit_multiplier=3)
        chat = limits.get_policy("chat")
        chat["window"].update(enabled=True, tokens=40_000, slow_tokens=0)
        limits.set_policy("chat", chat, None)
        from bananachat import db
        db.execute("UPDATE users SET music_opted_in=1 WHERE id=?", (user["id"],))
        credits.set_quota(user["id"], 30_000, 15_000, None)
        credits.charge(user["id"], 6000, 4000, request_type="api")
    browser = _signed_in(app, "anna")
    html = browser.get("/account").get_data(as_text=True)
    assert 'value="10000.0" max="90000.0"' in html     # 30k tokens x3, 10k used
    assert 'max="45000.0"' not in html                  # no second allowance
    assert 'value="0" max="120000.0"' in html or 'value="0.0" max="120000.0"' in html  # chat pool 40k x3
    assert "×3" in html


def test_admin_account_page_is_unlimited(admin):
    response = admin.get("/account")
    assert response.status_code == 200
    assert "<progress" not in response.get_data(as_text=True)


def test_password_change_keeps_this_device_and_signs_out_others(app, make_user):
    make_user("bob")
    laptop, phone = _signed_in(app, "bob"), _signed_in(app, "bob")
    response = laptop.post("/account/password", {"current_password": "bob-password", "new_password": "new-secret-99",
                                                  "confirm_password": "new-secret-99"})
    assert response.status_code == 302
    assert laptop.fetch("/account").status_code == 200
    assert phone.fetch("/account").status_code == 401
    Browser(app).login("bob", "new-secret-99")


def test_password_change_validates(app, make_user):
    from bananachat.db import users

    user = make_user("bea")
    browser = _signed_in(app, "bea")
    before = None
    with app.app_context():
        before = users.get(user["id"])["password"]
    browser.post("/account/password", {"current_password": "wrong", "new_password": "new-secret-99",
                                       "confirm_password": "new-secret-99"})
    browser.post("/account/password", {"current_password": "bea-password", "new_password": "short",
                                       "confirm_password": "short"})
    browser.post("/account/password", {"current_password": "bea-password", "new_password": "new-secret-99",
                                       "confirm_password": "different-99"})
    with app.app_context():
        assert users.get(user["id"])["password"] == before


def test_sessions_are_listed_and_can_be_revoked(app, make_user):
    from bananachat.db import users

    user = make_user("carl")
    laptop = _signed_in(app, "carl")
    phone = _signed_in(app, "carl")
    tablet = _signed_in(app, "carl")
    html = laptop.get("/account", headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/120.0"}) \
        .get_data(as_text=True)
    assert html.count("/revoke\"") == 2  # every session except this one
    with app.app_context():
        sessions = users.list_sessions(user["id"])
    with phone.client.session_transaction() as session:
        phone_hash = users.session_hash(session["sid"])
    assert phone_hash in {row["id_hash"] for row in sessions}
    assert laptop.post(f"/account/sessions/{phone_hash}/revoke").status_code == 302
    assert phone.fetch("/account").status_code == 401
    assert tablet.fetch("/account").status_code == 200
    laptop.post("/account/sessions/revoke-others")
    assert tablet.fetch("/account").status_code == 401
    assert laptop.fetch("/account").status_code == 200


def test_session_of_another_user_cannot_be_revoked(app, make_user):
    from bananachat.db import users

    make_user("dina")
    make_user("eric")
    dina, eric = _signed_in(app, "dina"), _signed_in(app, "eric")
    with eric.client.session_transaction() as session:
        eric_hash = users.session_hash(session["sid"])
    dina.post(f"/account/sessions/{eric_hash}/revoke")
    assert eric.fetch("/account").status_code == 200


def test_gdpr_export_contains_everything_but_secrets(app, make_user):
    from bananachat import db
    from bananachat.db import credits, personalities, tokens, users
    from bananachat.services import exports

    user = make_user("fay")
    with app.app_context():
        _token_id, raw = tokens.create(user["id"], "laptop")
        personalities.create(user["id"], "Pirate", "Talk like a pirate.", created_by=user["id"])
        credits.charge(user["id"], 1000, 1000, request_type="api")
        credits.submit_request(user["id"], 50_000, 20_000, "More work to do")
        with db.transaction():
            db.execute("INSERT INTO chat_sessions (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                       ("chat1", user["id"], "Hello chat", db.now(), db.now()))
            db.execute("INSERT INTO chat_sessions (id, user_id, title, created_at, updated_at, deleted_at) "
                       "VALUES (?,?,?,?,?,?)", ("chat2", user["id"], "Gone chat", db.now(), db.now(), db.now()))
            message = db.execute("INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                                 ("chat1", "user", "hi there", db.now())).lastrowid
            db.execute("INSERT INTO chat_attachments (id, message_id, kind, filename, media_type, size_bytes, sha256, "
                       "image_data, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       ("att1", message, "image", "a.png", "image/png", 3, "ab" * 32, b"\x89PN", db.now()))
        password_hash = users.get(user["id"])["password"]
        token_hash = tokens.hash_token(raw)

    browser = _signed_in(app, "fay")
    response = browser.post("/account/export/gdpr")
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    text = response.get_data(as_text=True)
    data = json.loads(text)
    for key in ("account", "preferences", "quota", "api_tokens", "credit_ledger", "quota_requests",
                "access_memberships", "access_requests", "personalities", "sign_in_sessions", "chats"):
        assert key in data, key
    assert data["account"]["username"] == "fay"
    assert data["api_tokens"][0]["name"] == "laptop"
    assert data["personalities"][0]["name"] == "Pirate"
    assert [chat["title"] for chat in data["chats"]] == ["Hello chat"]
    attachment = data["chats"][0]["messages"][0]["attachments"][0]
    assert attachment["image_included"] is True and attachment["image_base64"]
    assert data["deleted_chats_awaiting_erasure"] == 1
    assert password_hash not in text and token_hash not in text and raw not in text
    assert "password" not in data["account"] and "token_hash" not in text and "id_hash" not in text
    with browser.client.session_transaction() as session:
        from bananachat.db import users as users_db
        assert users_db.session_hash(session["sid"]) not in text

    with app.app_context():
        chats = exports.chats_export(user["id"])
    assert set(chats) >= {"chats", "export_type"} and "credit_ledger" not in chats
    assert "image_base64" not in json.dumps(chats)


def test_chats_export_download(app, make_user):
    make_user("gus")
    browser = _signed_in(app, "gus")
    response = browser.get("/account/export/chats")
    assert response.status_code == 200
    assert json.loads(response.get_data())["chats"] == []


def test_delete_account(app, make_user):
    from bananachat.db import users

    user = make_user("hana")
    browser = _signed_in(app, "hana")
    browser.post("/account/delete", {"confirm_username": "Hana", "password": "hana-password"})
    with app.app_context():
        assert users.get(user["id"]) is not None
    browser.post("/account/delete", {"confirm_username": "hana", "password": "wrong"})
    with app.app_context():
        assert users.get(user["id"]) is not None
    response = browser.post("/account/delete", {"confirm_username": "hana", "password": "hana-password"})
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    with app.app_context():
        assert users.get(user["id"]) is None
        assert users.list_audit(action="account.delete")[0]["actor_name"] == "hana"


def test_last_admin_cannot_delete_themself(app):
    from bananachat.db import users

    admin = _signed_in(app, "admin", "admin-password")
    admin.post("/account/delete", {"confirm_username": "admin", "password": "admin-password"})
    with app.app_context():
        assert users.get_by_username("admin") is not None
    assert admin.get("/account").status_code == 200


def test_quota_request_pending_path(app, make_user):
    from bananachat.db import credits

    user = make_user("ivan")
    browser = _signed_in(app, "ivan")
    browser.post("/account/quota-request", {"tokens": "60k", "slow_tokens": "15k", "reason": "Big project"})
    with app.app_context():
        pending = credits.pending_request(user["id"])
        assert pending is not None and pending["new_tokens"] == 60_000 and pending["new_credits"] == 60
        assert credits.get_quota(user["id"]) == (45_000, 0)
    html = browser.get("/account").get_data(as_text=True)
    assert "60k" in html
    browser.post("/account/quota-request", {"tokens": "70000", "slow_tokens": "15000", "reason": "Another one"})
    with app.app_context():
        assert len(credits.user_requests(user["id"])) == 1


def test_quota_request_automatic_approval(app, make_user):
    from bananachat.db import credits, settings

    user = make_user("jill")
    with app.app_context():
        settings.update(quota_auto_approve_enabled=1, quota_auto_approve_max_tokens=100_000,
                        quota_auto_approve_max_slow_tokens=50_000)
    browser = _signed_in(app, "jill")
    browser.post("/account/quota-request", {"tokens": "80k", "slow_tokens": "20k", "reason": "Testing things"})
    with app.app_context():
        assert credits.get_quota(user["id"]) == (80_000, 0)
        assert credits.user_requests(user["id"])[0]["status"] == "approved"


def test_quota_request_must_raise(app, make_user):
    from bananachat.db import credits

    user = make_user("kent")
    browser = _signed_in(app, "kent")
    browser.post("/account/quota-request", {"tokens": "10k", "slow_tokens": "15k", "reason": "Less please"})
    browser.post("/account/quota-request", {"tokens": "60k", "slow_tokens": "15k", "reason": "no"})
    with app.app_context():
        assert credits.user_requests(user["id"]) == []


def test_rejected_requests_keep_what_was_typed(app, make_user):
    make_user("mona")
    browser = _signed_in(app, "mona")
    response = browser.post("/account/quota-request", {"tokens": "2000000001", "slow_tokens": "15k",
                                                       "reason": "A long reason worth keeping"})
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert "A long reason worth keeping</textarea>" in html and 'value="2000000001"' in html
    form = {"resource": "uncensored:0", "use_case": "Research I would hate to type twice.", "confirmed_safe": "1"}
    response = browser.post("/account/access-request", form)
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert "Research I would hate to type twice.</textarea>" in html
    assert 'value="uncensored:0" selected' in html


def test_access_request_for_a_capability(app, make_user):
    from bananachat.db import access

    user = make_user("lena")
    browser = _signed_in(app, "lena")
    html = browser.get("/account").get_data(as_text=True)
    assert 'value="uncensored:0"' in html
    form = {"resource": "uncensored:0", "use_case": "Research on content moderation.", "confirmed_safe": "1",
            "confirmed_logging": "1"}
    browser.post("/account/access-request", {**form, "confirmed_logging": ""})
    with app.app_context():
        assert access.list_requests(user_id=user["id"]) == []
    browser.post("/account/access-request", form)
    with app.app_context():
        rows = access.list_requests(user_id=user["id"])
        assert len(rows) == 1 and rows[0]["scope"] == "uncensored"
        access.resolve_request(rows[0]["id"], None, False, "Not for now")
    html = browser.get("/account").get_data(as_text=True)
    assert "Not for now" in html
    # Allowed capabilities cannot be requested.
    browser.post("/account/access-request", {**form, "resource": "custom_personality:0"})
    with app.app_context():
        assert len(access.list_requests(status=None, user_id=user["id"])) == 1


def test_access_request_for_a_model_that_denies_the_user(app, make_user):
    from bananachat import db
    from bananachat.db import access

    user = make_user("mona")
    with app.app_context():
        model_id = db.execute(
            "INSERT INTO ai_models (ollama_name, backend, backend_model_name, backend_available, display_name, "
            "is_rolled_out, sort_order, created_at, updated_at) VALUES ('m:1','ollama','m:1',1,'Secret model',1,1,?,?)",
            (db.now(), db.now())).lastrowid
        hidden_id = db.execute(
            "INSERT INTO ai_models (ollama_name, backend, backend_model_name, backend_available, display_name, "
            "is_rolled_out, sort_order, created_at, updated_at) VALUES ('m:2','ollama','m:2',1,'Hidden model',0,2,?,?)",
            (db.now(), db.now())).lastrowid
        access.set_policy("model", model_id, "deny_except_allowlist", True, None)
        access.set_policy("model", hidden_id, "deny_except_allowlist", True, None)
    browser = _signed_in(app, "mona")
    html = browser.get("/account").get_data(as_text=True)
    assert "Secret model" in html and "Hidden model" not in html
    form = {"use_case": "I need it for my thesis.", "confirmed_safe": "1", "confirmed_logging": "1"}
    browser.post("/account/access-request", {**form, "resource": f"model:{hidden_id}"})
    browser.post("/account/access-request", {**form, "resource": f"model:{model_id}"})
    with app.app_context():
        rows = access.list_requests(user_id=user["id"])
        assert [(row["scope"], row["resource_id"]) for row in rows] == [("model", model_id)]


def test_delete_all_chats(app, make_user):
    chats = pytest.importorskip("bananachat.db.chats")
    if not hasattr(chats, "delete_all_for_user"):
        pytest.skip("chat storage not available yet")
    from bananachat import db

    user = make_user("nico")
    with app.app_context():
        db.execute("INSERT INTO chat_sessions (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                   ("c-nico", user["id"], "Mine", db.now(), db.now()))
    browser = _signed_in(app, "nico")
    browser.post("/account/chats/delete-all", {"password": "wrong"})
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM chat_sessions WHERE user_id=? AND deleted_at IS NULL",
                         (user["id"],)) == 1
    browser.post("/account/chats/delete-all", {"password": "nico-password"})
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM chat_sessions WHERE user_id=? AND deleted_at IS NULL",
                         (user["id"],), 0) == 0


def test_catalog_covers_every_key_used_by_these_pages():
    import re
    from pathlib import Path

    from bananachat.i18n import catalogs

    root = Path(__file__).resolve().parents[2] / "bananachat"
    sources = [*root.glob("templates/account/*.html"), *root.glob("templates/customization/*.html"),
               *root.glob("templates/personalities/*.html"), *root.glob("templates/music/*.html"),
               root / "templates/partials/music_player.html",
               *(root / "web" / f"{name}.py" for name in ("account", "customization", "personalities", "music"))]
    used = set()
    for source in sources:
        used |= set(re.findall(r"""(?<![A-Za-z])_?t\(["']((?:account|customize|personalities|music|auth|common|nav|time)\.[a-z0-9_]+)["']""",
                               source.read_text()))
    scripts = [*root.glob("static/js/account.js"), root / "static/js/customization.js",
               root / "static/js/music-player.js"]
    for script in scripts:
        used |= {f"js.{key}" for key in re.findall(r"""\bt\(["']([a-z0-9_]+)["']""", script.read_text())}
    table = catalogs()
    assert set(table["en"]) == set(table["it"])
    missing = sorted(key for key in used if key not in table["en"] and not key.endswith("_"))  # "_" = prefix
    assert not missing, missing
