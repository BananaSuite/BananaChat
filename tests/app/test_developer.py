"""The developer area: API tokens, usage, playground and old URLs."""

from __future__ import annotations

import hashlib
import re

import pytest

from tests.app.conftest import Browser
from tests.app.test_api import call, events, ledger, roll_out


@pytest.fixture
def dev(app, make_user):
    roll_out(app)
    user = make_user("devon")
    browser = Browser(app)
    browser.login("devon")
    browser.user = user
    return browser


def active_tokens(app, user):
    from bananachat.db import tokens

    with app.app_context():
        return tokens.list_for(user["id"])


# ----- tokens -----------------------------------------------------------------------------

def test_token_is_shown_once_and_only_its_hash_is_stored(app, dev):
    from bananachat import db

    response = dev.post_json("/developer/tokens", {"name": "  Laptop   script "})
    assert response.status_code == 201
    raw = response.json["token"]
    assert raw.startswith("bc-") and response.json["name"] == "Laptop script"
    assert response.headers["Cache-Control"] == "no-store"
    with app.app_context():
        row = db.one("SELECT * FROM api_tokens WHERE user_id=?", (dev.user["id"],))
        assert row["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in " ".join(str(value) for value in row.to_dict().values())
        assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='api_token.create'") == 1
    with dev.client.session_transaction() as session:
        assert raw not in repr(dict(session))
    page = dev.get("/developer").get_data(as_text=True)
    assert raw not in page and raw[:12] in page and "Laptop script" in page
    assert call(app, "GET", "/v1/models", raw).status_code == 200


def test_token_creation_without_javascript_shows_a_page(app, dev):
    response = dev.post("/developer/tokens", {"name": "Form"})
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    raw = re.search(r'readonly value="(bc-[^"]+)"', html).group(1)
    assert raw.startswith(active_tokens(app, dev.user)[0]["token_prefix"])
    assert response.headers["Cache-Control"] == "no-store"
    assert raw not in dev.get("/developer").get_data(as_text=True)
    assert call(app, "GET", "/v1/models", raw).status_code == 200


def test_token_limit(app, dev):
    from bananachat.db import tokens

    for index in range(tokens.MAX_PER_USER):
        assert dev.post_json("/developer/tokens", {"name": f"t{index}"}).status_code == 201
    response = dev.post_json("/developer/tokens", {"name": "one too many"})
    assert response.status_code == 400 and response.json["error"]["code"] == "token_limit"
    assert len(active_tokens(app, dev.user)) == tokens.MAX_PER_USER
    assert 'data-token-form="create"' not in dev.get("/developer").get_data(as_text=True)


def test_rotate_replaces_the_token(app, dev):
    old = dev.post_json("/developer/tokens", {"name": "ci"}).json
    response = dev.post_json(f"/developer/tokens/{old['id']}/rotate", {})
    assert response.status_code == 201
    new = response.json
    assert new["token"] != old["token"] and new["name"] == "ci"
    assert call(app, "GET", "/v1/models", old["token"]).status_code == 401
    assert call(app, "GET", "/v1/models", new["token"]).status_code == 200
    assert [row["id"] for row in active_tokens(app, dev.user)] == [new["id"]]


def test_rename_and_revoke(app, dev):
    token = dev.post_json("/developer/tokens", {"name": "old"}).json
    assert dev.post_json(f"/developer/tokens/{token['id']}/rename", {"name": "new name"}).json["name"] == "new name"
    assert dev.post_json(f"/developer/tokens/{token['id']}/rename", {"name": "  "}).status_code == 400
    assert active_tokens(app, dev.user)[0]["name"] == "new name"
    response = dev.post(f"/developer/tokens/{token['id']}/revoke")
    assert response.status_code == 302
    assert active_tokens(app, dev.user) == []
    assert call(app, "GET", "/v1/models", token["token"]).status_code == 401
    assert dev.post_json(f"/developer/tokens/{token['id']}/revoke", {}).status_code == 404


def test_tokens_of_other_users_cannot_be_touched(app, dev, make_user):
    from bananachat.db import tokens

    other = make_user("mallory")
    with app.app_context():
        token_id, raw = tokens.create(other["id"], "theirs")
    assert dev.post_json(f"/developer/tokens/{token_id}/rotate", {}).status_code == 404
    assert dev.post_json(f"/developer/tokens/{token_id}/revoke", {}).status_code == 404
    assert dev.post_json(f"/developer/tokens/{token_id}/rename", {"name": "mine"}).status_code == 404
    assert call(app, "GET", "/v1/models", raw).status_code == 200


def test_token_actions_need_csrf_and_a_session(app, dev):
    response = dev.client.post("/developer/tokens", json={"name": "x"}, headers={"X-Requested-With": "fetch"})
    assert response.status_code == 400
    anonymous = Browser(app)
    assert anonymous.post_json("/developer/tokens", {"name": "x"}).status_code == 401
    assert anonymous.get("/developer").status_code == 302


# ----- pages ----------------------------------------------------------------------------------

def test_overview_page(app, dev):
    from bananachat.db import credits, settings

    with app.app_context():
        settings.update(music_enabled=1, music_bonus_mode="multiplier", music_credit_multiplier=2)
        from bananachat import db
        db.execute("UPDATE users SET music_opted_in=1 WHERE id=?", (dev.user["id"],))
        credits.charge(dev.user["id"], 5000, 5000, request_type="api")
    html = dev.get("/developer").get_data(as_text=True)
    assert 'style="' not in html
    assert '<progress class="meter' in html and 'value="11.1"' in html  # 10k of 90k (45k x 2 bonus)
    assert 'value="http://localhost/v1"' in html and "OpenAI(base_url=&#34;http://localhost/v1&#34;" in html
    assert "llama3.2:3b" in html and "qwen3:4b" in html
    assert "developer.js" in html


def test_pages_are_translated(app, dev):
    from bananachat.db import users

    with app.app_context():
        prefs = users.get_preferences(dev.user["id"])
        prefs["interface_language"] = "it"
        users.save_preferences(dev.user["id"], prefs)
    for path in ("/developer", "/developer/usage", "/developer/playground"):
        html = dev.get(path).get_data(as_text=True)
        untranslated = set(re.findall(r"\b(?:developer|images)\.[a-z_]+", html)) - {"developer.css", "developer.js"}
        assert 'lang="it"' in html and not untranslated


def test_usage_history(app, dev):
    token = dev.post_json("/developer/tokens", {"name": "script"}).json
    call(app, "POST", "/v1/chat/completions", token["token"],
         {"model": "auto", "messages": [{"role": "user", "content": "Hi"}]})
    html = dev.get("/developer/usage").get_data(as_text=True)
    assert "script" in html and "Llama3.2" in html


def test_old_urls_redirect_permanently(dev):
    for old, new in (("/api", "/developer"), ("/api/usage", "/developer/usage"),
                     ("/api/playground", "/developer/playground")):
        response = dev.get(old)
        assert response.status_code == 301 and response.headers["Location"].endswith(new)


def test_admin_sees_unlimited_credits(app, admin):
    roll_out(app)
    html = admin.get("/developer").get_data(as_text=True)
    assert "<progress" not in html


# ----- playground -------------------------------------------------------------------------------

def test_playground_page_lists_text_models(dev):
    html = dev.get("/developer/playground").get_data(as_text=True)
    assert 'id="page-data"' in html and "playground.js" in html and "llama3.2:3b" in html
    assert 'style="' not in html


def test_playground_streams_and_charges(app, dev, fake_ollama):
    response = dev.post_json("/developer/playground/send", {
        "model": "auto", "messages": [{"role": "system", "content": "Be nice."}, {"role": "user", "content": "Hi"}],
        "temperature": 0.2, "stop": ["END"]}, headers={"Accept": "text/event-stream"})
    assert response.status_code == 200 and response.mimetype == "text/event-stream"
    items = events(response)
    kinds = [item["type"] for item in items]
    assert kinds[0] == "started" and kinds[-1] == "done"
    assert "".join(item["text"] for item in items if item["type"] == "delta") == "Hello from the fake model."
    done = items[-1]
    assert done["usage"]["prompt_tokens"] == 11 and done["tokens_counted"] > 0 and done["finish_reason"] == "stop"
    body = fake_ollama.chat_bodies()[-1]
    assert body["options"]["temperature"] == 0.2 and body["options"]["stop"] == ["END"]
    rows = ledger(app, dev.user)
    assert len(rows) == 1 and rows[0]["request_type"] == "playground" and rows[0]["token_id"] is None


def test_playground_errors_are_json_before_streaming(app, dev):
    from bananachat.db import credits

    response = dev.post_json("/developer/playground/send", {"model": "missing:1b",
                                                             "messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 404 and response.json["error"]["code"] == "model_not_found"
    assert dev.post_json("/developer/playground/send", {"messages": []}).status_code == 400
    with app.app_context():
        credits.set_quota(dev.user["id"], 0, 0, None)
    response = dev.post_json("/developer/playground/send", {"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 429 and response.headers["Retry-After"]


def test_playground_reports_backend_failures_in_the_stream(app, dev, fake_ollama):
    fake_ollama.reply = "one two three"
    fake_ollama.fail_models = {"llama3.2:3b", "qwen3:4b"}
    response = dev.post_json("/developer/playground/send", {"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 502
    assert ledger(app, dev.user) == []


def test_playground_requires_csrf(app, dev):
    response = dev.client.post("/developer/playground/send", json={"messages": [{"role": "user", "content": "Hi"}]},
                               headers={"X-Requested-With": "fetch"})
    assert response.status_code == 400


def test_playground_shares_the_api_rate_limit(app, dev):
    from bananachat.db import limits

    with app.app_context():
        policy = limits.get_policy("api")
        policy["rate"]["rules"] = [{"requests": 1, "per": "minute"}]
        limits.set_policy("api", policy, None)
    body = {"messages": [{"role": "user", "content": "Hi"}]}
    assert dev.post_json("/developer/playground/send", body).status_code == 200
    response = dev.post_json("/developer/playground/send", body)
    assert response.status_code == 429 and 45 <= int(response.headers["Retry-After"]) <= 60


def test_playground_is_paused_during_maintenance(app, dev):
    from bananachat.db import settings

    with app.app_context():
        settings.update(maintenance_mode=1)
    html = dev.get("/developer/playground").get_data(as_text=True)
    assert 'id="pg-send" disabled' in html
    response = dev.post_json("/developer/playground/send", {"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 503 and response.json["error"]["code"] == "maintenance"
    assert dev.get("/developer").status_code == 200  # tokens stay manageable
    assert ledger(app, dev.user) == []
