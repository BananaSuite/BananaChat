"""Personalities: access gating, create/edit/toggle/delete, moderation, and the version-7 features
(presentation fields, default personality, featured personalities, share links, import/export,
preferred models, response style)."""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from tests.app.conftest import TEST_CSRF, Browser
from tests.app.test_chat import new_chat, send, setup_models


def _signed_in(app, username):
    browser = Browser(app)
    browser.login(username)
    with browser.client.session_transaction() as session:
        session["csrf"] = TEST_CSRF
    return browser


def _deny(app, user_id):
    from bananachat.db import access

    with app.app_context():
        access.add_membership("custom_personality", 0, user_id, "denylist", added_by=None)


def test_create_edit_toggle_delete(app, make_user):
    from bananachat.db import personalities

    user = make_user("ada")
    browser = _signed_in(app, "ada")
    assert browser.get("/personalities").status_code == 200
    browser.post("/personalities", {"name": "Pirate", "instructions": "Talk like a pirate.", "enabled": "1"})
    with app.app_context():
        rows = personalities.list_for(user["id"])
    assert [row["name"] for row in rows] == ["Pirate"] and rows[0]["is_enabled"] == 1
    personality_id = rows[0]["id"]

    # Duplicate names (any case) and empty instructions are refused.
    browser.post("/personalities", {"name": "pirate", "instructions": "Again", "enabled": "1"})
    browser.post("/personalities", {"name": "Empty", "instructions": "   ", "enabled": "1"})
    with app.app_context():
        assert len(personalities.list_for(user["id"])) == 1

    browser.post(f"/personalities/{personality_id}", {"name": "Captain", "instructions": "Be a captain."})
    with app.app_context():
        row = personalities.get(personality_id)
    assert row["name"] == "Captain" and row["is_enabled"] == 0  # unchecked box switches it off

    browser.post(f"/personalities/{personality_id}/toggle", {"enabled": "1"})
    with app.app_context():
        assert personalities.get(personality_id)["is_enabled"] == 1
    html = browser.get("/personalities").get_data(as_text=True)
    assert "Captain" in html and "Be a captain." in html

    browser.post(f"/personalities/{personality_id}/delete")
    with app.app_context():
        assert personalities.get(personality_id) is None


def test_limit_is_enforced(app, make_user):
    from bananachat.db import personalities

    user = make_user("ben")
    with app.app_context():
        for index in range(personalities.MAX_PER_USER):
            personalities.create(user["id"], f"P{index}", "x", created_by=user["id"])
    browser = _signed_in(app, "ben")
    browser.post("/personalities", {"name": "One more", "instructions": "x", "enabled": "1"})
    with app.app_context():
        assert len(personalities.list_for(user["id"])) == personalities.MAX_PER_USER


def test_without_access_only_disable_and_delete_work(app, make_user):
    from bananachat.db import personalities

    user = make_user("cleo")
    with app.app_context():
        kept = personalities.create(user["id"], "Kept", "Keep me", created_by=user["id"])
        gone = personalities.create(user["id"], "Gone", "Delete me", created_by=user["id"])
    _deny(app, user["id"])
    browser = _signed_in(app, "cleo")
    html = browser.get("/personalities").get_data(as_text=True)
    assert "/account#access" in html and 'action="/personalities"' not in html

    browser.post("/personalities", {"name": "New", "instructions": "x", "enabled": "1"})
    browser.post(f"/personalities/{kept}", {"name": "Renamed", "instructions": "x", "enabled": "1"})
    browser.post(f"/personalities/{kept}/toggle", {"enabled": "0"})
    with app.app_context():
        row = personalities.get(kept)
        assert row["name"] == "Kept" and row["is_enabled"] == 0
        assert len(personalities.list_for(user["id"])) == 2
    browser.post(f"/personalities/{kept}/toggle", {"enabled": "1"})
    browser.post(f"/personalities/{gone}/delete")
    with app.app_context():
        assert personalities.get(kept)["is_enabled"] == 0
        assert personalities.get(gone) is None


def test_other_users_personalities_are_not_reachable(app, make_user):
    from bananachat.db import personalities

    owner = make_user("dora")
    make_user("evan")
    with app.app_context():
        personality_id = personalities.create(owner["id"], "Mine", "Private", created_by=owner["id"])
    intruder = _signed_in(app, "evan")
    assert intruder.post(f"/personalities/{personality_id}/delete").status_code == 404
    assert intruder.post(f"/personalities/{personality_id}", {"name": "x", "instructions": "y"}).status_code == 404
    assert "Private" not in intruder.get("/personalities").get_data(as_text=True)
    with app.app_context():
        assert personalities.get(personality_id) is not None


def test_moderation_state_is_shown(app, make_user):
    from bananachat.db import personalities

    user = make_user("finn")
    until = datetime.now(timezone.utc) + timedelta(days=3)
    with app.app_context():
        personality_id = personalities.create(user["id"], "Loud", "Shout", created_by=user["id"])
        personalities.moderate(personality_id, disabled=True, updated_by=None, until=until, reason="Too loud")
    html = _signed_in(app, "finn").get("/personalities").get_data(as_text=True)
    assert "Too loud" in html and until.strftime("%Y-%m-%d") in html


# ----- richer personalities (schema version 7) --------------------------------------------------

FULL_FORM = {
    "name": "Owl", "description": "A patient tutor.", "avatar": "🦉", "color": "green",
    "instructions": "Explain step by step.", "greeting": "Hello, learner!", "starter_1": "Teach me fractions",
    "starter_2": "", "starter_3": "Quiz me", "starter_4": "", "preferred_model": "llama3.2:3b",
    "response_length": "concise", "creativity": "precise", "enabled": "1",
}


def _row(app, personality_id):
    from bananachat.db import personalities

    with app.app_context():
        return personalities.get(personality_id)


def _own(app, user_id):
    from bananachat.db import personalities

    with app.app_context():
        return personalities.list_for(user_id)


def _featured(app, name="Guide", **fields):
    from bananachat.db import personalities, users

    with app.app_context():
        admin = users.get_by_username("admin")
        return personalities.create(admin["id"], name, f"{name} instructions.", created_by=admin["id"],
                                    kind="featured", **fields)


def test_migration_keeps_existing_personalities_working(make_app, tmp_path):
    from bananachat import db
    from bananachat.db import personalities
    from bananachat.services import personalities as service
    from tests.app.test_foundation import legacy_instance

    app = make_app(setup=False, INSTANCE_DIR=str(legacy_instance(tmp_path)))
    with app.app_context():
        assert db.schema_version() == db.SCHEMA_VERSION
        row = db.one("SELECT * FROM personalities WHERE name='Pirate'")
        assert (row["kind"], row["avatar"], row["color"], row["description"], row["greeting"]) == \
            ("user", "", "", "", "")
        assert (row["response_length"], row["creativity"], row["preferred_model"]) == ("balanced", "balanced", "")
        assert personalities.starters(row) == [] and row["share_token"] is None
        assert [item["name"] for item in personalities.list_for(row["user_id"])] == ["Pirate"]
        # The balanced style adds nothing: the prompt is exactly the old instructions.
        assert service.prompt_text(row) == "Talk like a pirate."
        assert db.scalar("SELECT COUNT(*) FROM personality_defaults", (), 0) == 0


def test_full_form_is_saved_and_shown(app, make_user):
    user = make_user("gina")
    setup_models(app)
    browser = _signed_in(app, "gina")
    response = browser.post("/personalities", FULL_FORM)
    assert response.status_code == 302 and "#personality-" in response.headers["Location"]
    (row,) = _own(app, user["id"])
    assert (row["avatar"], row["color"], row["description"], row["greeting"]) == \
        ("🦉", "green", "A patient tutor.", "Hello, learner!")
    assert json.loads(row["starters"]) == ["Teach me fractions", "Quiz me"]
    assert (row["preferred_model"], row["response_length"], row["creativity"]) == ("llama3.2:3b", "concise", "precise")
    edit = browser.get(f"/personalities/{row['id']}/edit").get_data(as_text=True)
    assert 'value="Teach me fractions"' in edit and 'value="green" checked' in edit
    assert re.search(r'<option value="llama3.2:3b" selected', edit)


@pytest.mark.parametrize(("field", "value"), [
    ("avatar", "ab"), ("avatar", "🦉🦊"), ("avatar", "<b>"), ("color", "#ff0000"), ("color", "red; x: y"),
    ("description", "d" * 161), ("greeting", "g" * 601), ("starter_1", "s" * 201), ("response_length", "huge"),
    ("creativity", "wild"), ("preferred_model", "not-a-model:1b"), ("name", ""), ("instructions", " "),
])
def test_invalid_fields_are_refused_and_the_typed_text_kept(app, make_user, field, value):
    user = make_user("hugo")
    setup_models(app)
    browser = _signed_in(app, "hugo")
    response = browser.post("/personalities", {**FULL_FORM, "greeting": "Typed greeting", field: value})
    assert response.status_code == 400
    page = response.get_data(as_text=True)
    if field != "greeting":
        assert "Typed greeting" in page
    assert _own(app, user["id"]) == []


def test_emoji_validation():
    from bananachat.services.personalities import is_emoji

    for good in ("🦉", "✨", "🛠️", "👩‍💻", "🏳️‍🌈", "🇮🇹", "👍🏽", "1️⃣", "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "❤"):
        assert is_emoji(good), good
    for bad in ("", "a", "🦉 ", "🦉a", "🇮", "🇮🇹🇮🇹", "‍", "🦉‍", "<", "1", "🦉" * 2, "x" * 100, None, 5):
        assert not is_emoji(bad), bad


def test_default_personality_for_new_chats(app, make_user):
    from bananachat.db import chats, personalities

    user = make_user("ivy")
    setup_models(app)
    browser = _signed_in(app, "ivy")
    with app.app_context():
        own = personalities.create(user["id"], "Mine", "Be mine.", created_by=user["id"])
        off = personalities.create(user["id"], "Off", "Off.", created_by=user["id"], enabled=False)
    featured = _featured(app)

    assert browser.post("/personalities/default", {"personality_id": str(off)}).status_code == 302
    with app.app_context():
        assert personalities.default_id(user["id"]) is None  # a switched-off one cannot be the default
    browser.post("/personalities/default", {"personality_id": str(own)})
    session_id = new_chat(browser)
    with app.app_context():
        assert chats.get(session_id)["personality_id"] == own
    # "New chat" reuses the empty chat but starts it again with the default.
    browser.post_json(f"/chat/{session_id}/personality", {"personality_id": None})
    assert new_chat(browser) == session_id
    with app.app_context():
        assert chats.get(session_id)["personality_id"] == own

    browser.post("/personalities/default", {"personality_id": str(featured)})
    with app.app_context():
        assert personalities.default_id(user["id"]) == featured
    assert 'value="%d" selected' % featured in browser.get("/personalities").get_data(as_text=True)
    browser.post("/personalities/default", {"personality_id": ""})
    new_chat(browser)
    with app.app_context():
        assert personalities.default_id(user["id"]) is None
        assert chats.get(session_id)["personality_id"] is None

    # "Try it" starts a chat with the chosen personality.
    response = browser.post("/chat/new", {"personality_id": str(own)})
    with app.app_context():
        assert chats.get(response.headers["Location"].rsplit("/", 1)[1])["personality_id"] == own
    # Deleting the default removes it.
    browser.post("/personalities/default", {"personality_id": str(own)})
    browser.post(f"/personalities/{own}/delete")
    with app.app_context():
        assert personalities.default_id(user["id"]) is None


def test_featured_personalities_are_read_only_and_can_be_duplicated(app, make_user):
    from bananachat.db import personalities

    user = make_user("jack")
    setup_models(app)
    featured = _featured(app, "Guide", avatar="🧭", description="Shows the way <b>here</b>",
                         starters=["Where to?"], preferred_model="llama3.2:3b")
    draft = _featured(app, "Hidden draft")
    with app.app_context():
        personalities.set_enabled(draft, False, None)
    browser = _signed_in(app, "jack")
    page = browser.get("/personalities").get_data(as_text=True)
    assert "Guide" in page and "Shows the way &lt;b&gt;here&lt;/b&gt;" in page and "Hidden draft" not in page
    assert f"/personalities/{featured}/edit" not in page

    # Not editable, not deletable, not exportable by users.
    assert browser.get(f"/personalities/{featured}/edit").status_code == 404
    assert browser.post(f"/personalities/{featured}", {"name": "x", "instructions": "y"}).status_code == 404
    assert browser.post(f"/personalities/{featured}/delete").status_code == 404
    assert browser.get(f"/personalities/{featured}/export").status_code == 404
    assert browser.post(f"/personalities/{draft}/duplicate").status_code == 404

    assert browser.post(f"/personalities/{featured}/duplicate").status_code == 302
    browser.post(f"/personalities/{featured}/duplicate")
    names = sorted(row["name"] for row in _own(app, user["id"]))
    assert names == ["Guide", "Guide (2)"]
    copy = next(row for row in _own(app, user["id"]) if row["name"] == "Guide")
    assert copy["kind"] == "user" and copy["avatar"] == "🧭" and json.loads(copy["starters"]) == ["Where to?"]
    # A copy, not a reference: later edits of the original do not reach it.
    with app.app_context():
        personalities.update(featured, name="Guide", instructions="Changed.", enabled=True, updated_by=None)
    assert _row(app, copy["id"])["instructions"] == "Guide instructions."

    # Featured personalities can be used in chats and are layered like the user's own.
    session_id = new_chat(browser)
    assert browser.post_json(f"/chat/{session_id}/personality", {"personality_id": featured}).status_code == 200
    assert browser.post_json(f"/chat/{session_id}/personality", {"personality_id": draft}).status_code == 403


def test_featured_personalities_survive_deleting_their_administrator(app, make_user):
    from bananachat.db import personalities, users

    second = make_user("root2", role="admin")
    with app.app_context():
        featured = personalities.create(second["id"], "Keeper", "Keep.", created_by=second["id"], kind="featured")
        users.delete(second["id"])
        row = personalities.get(featured)
    assert row is not None and row["username"] == "admin" and row["kind"] == "featured"


def test_share_link_preview_copy_and_revoke(app, make_user, browser_for):
    from bananachat.db import personalities

    owner = make_user("kate")
    other = make_user("liam")
    setup_models(app)
    with app.app_context():
        personality_id = personalities.create(owner["id"], "Poet <i>", "Rhyme <script>alert(1)</script>",
                                              created_by=owner["id"], greeting="Hi <b>there</b>")
    kate = _signed_in(app, "kate")
    response = kate.post_json(f"/personalities/{personality_id}/share")
    url = response.get_json()["url"]
    path = url.split("localhost", 1)[1]
    assert kate.post(f"/personalities/{personality_id}/share").status_code == 302
    assert _row(app, personality_id)["share_token"] in url  # the same link until revoked
    assert path in kate.get("/personalities").get_data(as_text=True)

    # Signed-out visitors must sign in; the page is escaped and not indexed.
    anonymous = browser_for()
    assert anonymous.get(path).status_code in (302, 401)
    liam = _signed_in(app, "liam")
    response = liam.get(path)
    page = response.get_data(as_text=True)
    assert response.status_code == 200 and response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "Hi &lt;b&gt;there&lt;/b&gt;" in page and "Poet &lt;i&gt;" in page

    assert liam.post(path + "/save").status_code == 302
    (copy,) = _own(app, other["id"])
    assert copy["name"] == "Poet <i>" and copy["user_id"] == other["id"] and copy["share_token"] is None
    # Using the original by id is still refused: it belongs to someone else.
    session_id = new_chat(liam)
    assert liam.post_json(f"/chat/{session_id}/personality", {"personality_id": personality_id}).status_code == 403

    # Only the owner can share or revoke.
    assert liam.post(f"/personalities/{personality_id}/unshare").status_code == 404
    assert liam.post(f"/personalities/{personality_id}/share").status_code == 404
    kate.post(f"/personalities/{personality_id}/unshare")
    assert liam.get(path).status_code == 404
    assert liam.post(path + "/save").status_code == 404
    assert _row(app, copy["id"]) is not None  # copies stay

    # A link to a personality disabled by an administrator stops working meanwhile.
    token = kate.post_json(f"/personalities/{personality_id}/share").get_json()["url"].rsplit("/", 1)[1]
    with app.app_context():
        personalities.moderate(personality_id, disabled=True, updated_by=None, reason="Spam")
    assert liam.get(f"/personalities/shared/{token}").status_code == 404
    assert kate.post(f"/personalities/{personality_id}/share").status_code == 302
    assert liam.get("/personalities/shared/not-a-real-token-at-all").status_code == 404


def test_export_and_import_round_trip(app, make_user):
    from bananachat.services import personalities as service

    user = make_user("mia")
    setup_models(app)
    browser = _signed_in(app, "mia")
    browser.post("/personalities", FULL_FORM)
    (row,) = _own(app, user["id"])
    response = browser.get(f"/personalities/{row['id']}/export")
    assert response.status_code == 200 and "attachment" in response.headers["Content-Disposition"]
    document = json.loads(response.get_data(as_text=True))
    assert document["format"] == service.EXPORT_FORMAT and document["version"] == 1
    assert document["personality"]["starters"] == ["Teach me fractions", "Quiz me"]
    assert "id" not in document["personality"] and "user_id" not in document["personality"]

    def upload(body: bytes, name="p.json"):
        return browser.post("/personalities/import", {"file": (io.BytesIO(body), name)},
                            content_type="multipart/form-data")

    assert upload(response.get_data()).status_code == 302
    rows = sorted(_own(app, user["id"]), key=lambda item: item["id"])
    assert [item["name"] for item in rows] == ["Owl", "Owl (2)"]
    imported = rows[1]
    for field in ("description", "avatar", "color", "instructions", "greeting", "starters", "preferred_model",
                  "response_length", "creativity"):
        assert imported[field] == row[field], field


HOSTILE = [
    b"",
    b"not json",
    b"[1, 2, 3]",
    b'{"format": "something.else", "version": 1, "personality": {}}',
    b'{"format": "bananachat.personality", "version": 99, "personality": {"name": "a", "instructions": "b"}}',
    b'{"format": "bananachat.personality", "version": "1", "personality": {"name": "a", "instructions": "b"}}',
    b'{"format": "bananachat.personality", "version": 1, "personality": "x"}',
    b'{"format": "bananachat.personality", "version": 1, "personality": {"name": ["a"], "instructions": "b"}}',
    b'{"format": "bananachat.personality", "version": 1, "personality": {"name": "a", "instructions": "b", '
    b'"starters": "not a list"}}',
    b'{"format": "bananachat.personality", "version": 1, "personality": {"name": "a", "instructions": "b", '
    b'"starters": [1, 2]}}',
    b'{"format": "bananachat.personality", "version": 1, "personality": {"name": "a", "instructions": "b", '
    b'"starters": ["1", "2", "3", "4", "5"]}}',
    b'{"format": "bananachat.personality", "version": 1, "personality": {"name": "a", "instructions": "b", '
    b'"color": "red; background: url(x)"}}',
    b'{"format": "bananachat.personality", "version": 1, "personality": {"name": "a", "instructions": "b", '
    b'"avatar": "<img src=x>"}}',
    b'{"format": "bananachat.personality", "version": 1, "personality": {"name": "a", "instructions": NaN}}',
    b'{"format": "bananachat.personality", "version": 1, "personality": {"name": "' + b"n" * 81 + b'", '
    b'"instructions": "b"}}',
    b'{"format": "bananachat.personality", "version": 1, "personality": {"name": "a", "instructions": "'
    + b"i" * 8001 + b'"}}',
    b"[" * 100000 + b"]" * 100000,
    b'{"format": "bananachat.personality", "version": 1, "personality": {"name": "a", "instructions": "b", '
    b'"pad": "' + b"x" * 70000 + b'"}}',
    b"\xff\xfe\x00garbage",
]


@pytest.mark.parametrize("body", HOSTILE, ids=range(len(HOSTILE)))
def test_hostile_imports_are_refused(app, make_user, body):
    user = make_user("nora")
    browser = _signed_in(app, "nora")
    response = browser.post("/personalities/import", {"file": (io.BytesIO(body), "evil.json")},
                            content_type="multipart/form-data")
    assert response.status_code in (302, 413)
    assert _own(app, user["id"]) == []


def test_import_drops_a_model_the_user_cannot_use(app, make_user):
    from bananachat.db import catalog

    user = make_user("olive")
    setup_models(app)
    with app.app_context():
        catalog.set_rollout(catalog.get_by_name("qwen3:4b")["id"], False)
    body = json.dumps({"format": "bananachat.personality", "version": 1, "personality": {
        "name": "Q", "instructions": "Use qwen.", "preferred_model": "qwen3:4b", "extra": {"ignored": True}}})
    browser = _signed_in(app, "olive")
    browser.post("/personalities/import", {"file": (io.BytesIO(body.encode()), "q.json")},
                 content_type="multipart/form-data")
    (row,) = _own(app, user["id"])
    assert row["name"] == "Q" and row["preferred_model"] == ""


def test_preferred_model_access_rules(app, make_user):
    from bananachat.db import catalog, personalities

    user = make_user("paul")
    setup_models(app)
    browser = _signed_in(app, "paul")
    with app.app_context():
        qwen = catalog.get_by_name("qwen3:4b")
        catalog.set_rollout(qwen["id"], False)
    # A model the user may not use cannot be chosen...
    assert browser.post("/personalities", {**FULL_FORM, "preferred_model": "qwen3:4b"}).status_code == 400
    assert browser.post("/personalities", FULL_FORM).status_code == 302
    (row,) = _own(app, user["id"])
    # ...but one that became unavailable later is kept while other fields are edited, and flagged.
    with app.app_context():
        personalities.update(row["id"], name="Owl", instructions="x", enabled=True, updated_by=None,
                             preferred_model="qwen3:4b")
    assert browser.post(f"/personalities/{row['id']}", {**FULL_FORM, "preferred_model": "qwen3:4b",
                                                        "description": "Edited"}).status_code == 302
    assert _row(app, row["id"])["description"] == "Edited"
    assert "chip-warning" in browser.get("/personalities").get_data(as_text=True)

    # In the chat the preferred model is offered only when usable; the page says so otherwise.
    session_id = new_chat(browser)
    browser.post_json(f"/chat/{session_id}/personality", {"personality_id": row["id"]})
    page = browser.get(f"/chat/{session_id}").get_data(as_text=True)
    data = json.loads(re.search(r'<script type="application/json" id="page-data">(.*?)</script>', page, re.S).group(1))
    (entry,) = data["personalities"]
    assert entry["preferred_model"] == "qwen3:4b" and entry["preferred_available"] is False
    with app.app_context():
        catalog.set_rollout(qwen["id"], True)
    page = browser.get(f"/chat/{session_id}").get_data(as_text=True)
    data = json.loads(re.search(r'<script type="application/json" id="page-data">(.*?)</script>', page, re.S).group(1))
    assert data["personalities"][0]["preferred_available"] is True


def test_style_is_applied_as_options_and_a_short_instruction(app, make_user, fake_ollama):
    from bananachat.db import catalog, personalities

    user = make_user("quinn")
    setup_models(app)
    browser = _signed_in(app, "quinn")
    with app.app_context():
        model = catalog.get_by_name("llama3.2:3b")
        catalog.update(model["id"], temperature=0.5, system_prompt="Site rules come first.")
        precise = personalities.create(user["id"], "Precise", "Be exact.", created_by=user["id"],
                                       response_length="concise", creativity="precise")
        creative = personalities.create(user["id"], "Creative", "Be wild.", created_by=user["id"],
                                        response_length="detailed", creativity="creative")
        plain = personalities.create(user["id"], "Plain", "Be plain.", created_by=user["id"])
    session_id = new_chat(browser)

    def chat_with(personality_id, **fields):
        browser.post_json(f"/chat/{session_id}/personality", {"personality_id": personality_id})
        send(browser, session_id, model="llama3.2:3b", **fields).get_data()
        body = fake_ollama.chat_bodies()[-1]
        return body["options"], body["messages"][0]["content"]

    options, system = chat_with(precise)
    assert options["temperature"] == 0.2
    assert system.index("Site rules come first.") < system.index("Be exact.")
    assert "Apply them only where they do not conflict with the instructions above" in system
    assert "keep answers short" in system
    options, system = chat_with(creative)
    assert options["temperature"] == 0.8 and "thorough" in system
    options, system = chat_with(plain)
    assert options["temperature"] == 0.5 and system.endswith("Be plain.")
    # A temperature the user sets for the message wins over the personality's style.
    options, _system = chat_with(precise, temperature="1.1")
    assert options["temperature"] == 1.1
    # Raw options in a personality are impossible: unknown fields are rejected by the store.
    with app.app_context(), pytest.raises(TypeError):
        personalities.create(user["id"], "Raw", "x", created_by=user["id"], num_ctx=999999)


def test_style_temperature_stays_within_the_model_bounds():
    from bananachat.services.personalities import style_temperature

    assert style_temperature({"temperature": None}, "balanced") is None
    assert style_temperature({"temperature": None}, "precise") == 0.32
    assert style_temperature({"temperature": 1.9}, "creative") == 2.0
    assert style_temperature({"temperature": 0.0}, "creative") == 0.3
    assert style_temperature({"temperature": 0.0}, "precise") == 0.0


def test_moderation_works_for_every_kind(app, admin, make_user):
    from bananachat.db import personalities

    user = make_user("rosa")
    setup_models(app)
    featured = _featured(app, "Shared guide")
    with app.app_context():
        own = personalities.create(user["id"], "Mine", "Mine.", created_by=user["id"])
        personalities.share(own)
    rosa = _signed_in(app, "rosa")
    session_id = new_chat(rosa)
    for personality_id in (own, featured):
        admin.post(f"/admin/personalities/{personality_id}/disable", {"reason": "Rules"})
        assert rosa.post_json(f"/chat/{session_id}/personality", {"personality_id": personality_id}).status_code == 403
        admin.post(f"/admin/personalities/{personality_id}/enable")
        assert rosa.post_json(f"/chat/{session_id}/personality", {"personality_id": personality_id}).status_code == 200
    assert "Shared by link" in admin.get("/admin/personalities?kind=shared").get_data(as_text=True)
    admin.post(f"/admin/personalities/{own}/unshare")
    assert _row(app, own)["share_token"] is None
    for personality_id in (own, featured):
        assert admin.post(f"/admin/personalities/{personality_id}/delete").status_code == 302
        assert _row(app, personality_id) is None
    with app.app_context():
        from bananachat.db import chats

        assert chats.get(session_id)["personality_id"] is None
    actions = [row["action"] for row in _audit(app)]
    assert {"admin.personality_disable", "admin.personality_enable", "admin.personality_unshare",
            "admin.personality_delete"} <= set(actions)


def _audit(app):
    from bananachat import db

    with app.app_context():
        return db.query("SELECT action, target FROM audit_log ORDER BY id")


def test_admin_creates_edits_and_publishes_featured_personalities(app, admin, make_user):
    make_user("sam")
    setup_models(app)
    page = admin.get("/admin/personalities?kind=featured").get_data(as_text=True)
    assert "No featured personalities yet" in page  # none are seeded
    assert admin.get("/admin/personalities/featured/new").status_code == 200
    bad = admin.post("/admin/personalities/featured", {**FULL_FORM, "avatar": "no", "greeting": "Kept greeting"})
    assert bad.status_code == 400 and "Kept greeting" in bad.get_data(as_text=True)
    form = {**FULL_FORM, "enabled": ""}
    assert admin.post("/admin/personalities/featured", form).status_code == 302
    assert admin.post("/admin/personalities/featured", form).status_code == 400  # duplicate name
    from bananachat import db

    with app.app_context():
        row = db.one("SELECT * FROM personalities WHERE kind='featured'")
    assert row["is_enabled"] == 0
    sam = _signed_in(app, "sam")
    assert "Owl" not in sam.get("/personalities").get_data(as_text=True)  # a draft
    admin.post(f"/admin/personalities/{row['id']}/publish", {"published": "1"})
    assert "Owl" in sam.get("/personalities").get_data(as_text=True)
    assert admin.post(f"/admin/personalities/{row['id']}/edit", {**FULL_FORM, "description": "New words"}
                      ).status_code == 302
    assert _row(app, row["id"])["description"] == "New words"
    # Users' personalities are moderated, never edited, by administrators.
    with app.app_context():
        from bananachat.db import personalities, users

        sam_id = users.get_by_username("sam")["id"]
        private = personalities.create(sam_id, "Private", "x", created_by=sam_id)
    assert admin.get(f"/admin/personalities/{private}/edit").status_code == 404
    assert admin.post(f"/admin/personalities/{private}/publish", {"published": "1"}).status_code == 404
    actions = {item["action"] for item in _audit(app)}
    assert {"admin.personality_featured_create", "admin.personality_publish",
            "admin.personality_featured_update"} <= actions
    sam.post(f"/personalities/{row['id']}/duplicate")
    assert any(item["action"] == "personality.duplicate" for item in _audit(app))


def test_access_policy_gates_featured_and_shared_personalities(app, make_user):
    from bananachat.db import personalities

    owner = make_user("tina")
    user = make_user("uma")
    setup_models(app)
    featured = _featured(app)
    with app.app_context():
        shared = personalities.create(owner["id"], "Shared", "x", created_by=owner["id"])
        token = personalities.share(shared)
    _deny(app, user["id"])
    uma = _signed_in(app, "uma")
    page = uma.get("/personalities").get_data(as_text=True)
    assert "Guide" not in page and 'action="/personalities/import"' not in page
    assert uma.post(f"/personalities/{featured}/duplicate").status_code == 302
    assert uma.post(f"/personalities/shared/{token}/save").status_code == 302
    assert uma.post("/personalities/import", {"file": (io.BytesIO(b"{}"), "x.json")},
                    content_type="multipart/form-data").status_code == 302
    assert _own(app, user["id"]) == []
    session_id = new_chat(uma)
    assert uma.post_json(f"/chat/{session_id}/personality", {"personality_id": featured}).status_code == 403
    assert uma.post("/personalities/default", {"personality_id": str(featured)}).status_code == 302
    with app.app_context():
        assert personalities.default_id(user["id"]) is None


def test_rate_limits_on_sharing_and_importing(app, make_user):
    from bananachat.db import personalities

    user = make_user("vera")
    with app.app_context():
        personality_id = personalities.create(user["id"], "Mine", "x", created_by=user["id"])
    browser = _signed_in(app, "vera")
    statuses = [browser.post(f"/personalities/{personality_id}/share").status_code for _ in range(21)]
    assert statuses[:20] == [302] * 20 and statuses[20] == 429
    statuses = [browser.post("/personalities/import", {"file": (io.BytesIO(b"x"), "x.json")},
                             content_type="multipart/form-data").status_code for _ in range(21)]
    assert statuses[20] == 429


@pytest.mark.parametrize(("lang", "words"), [
    ("en", ["Personalities", "Start from a template", "Default for new chats", "Friendly tutor"]),
    ("it", ["Personalità", "Parti da un modello", "Predefinita per le nuove chat", "Tutor paziente"]),
])
def test_pages_are_translated(app, make_user, lang, words):
    from bananachat.db import personalities

    user = make_user("wes")
    setup_models(app)
    with app.app_context():
        personality_id = personalities.create(user["id"], "Mine", "x", created_by=user["id"])
        token = personalities.share(personality_id)
    browser = _signed_in(app, "wes")
    headers = {"Accept-Language": lang}
    page = browser.get("/personalities", headers=headers).get_data(as_text=True)
    assert all(word in page for word in words[:3])
    editor = browser.get("/personalities/new?template=friendly_tutor", headers=headers).get_data(as_text=True)
    assert words[3] in editor
    for url in (f"/personalities/{personality_id}/edit", f"/personalities/shared/{token}"):
        response = browser.get(url, headers=headers)
        assert response.status_code == 200 and f'lang="{lang}"' in response.get_data(as_text=True)
    for html in (page, editor):
        assert 'style="' not in html and "onclick" not in html and "<script>" not in html


def test_personality_catalog_covers_every_key_used():
    from pathlib import Path

    from bananachat.i18n import catalogs

    root = Path(__file__).resolve().parents[2] / "bananachat"
    sources = [*root.glob("templates/personalities/*.html"), root / "templates/chat/_composer.html",
               root / "web/personalities.py", root / "services/personalities.py"]
    used = set()
    for source in sources:
        used |= set(re.findall(r"""["'](personality\.[a-z0-9_]+)["']""", source.read_text()))
    for script in ("personalities.js", "chat-personas.js"):
        used |= {f"js.{key}" for key in re.findall(r"""\bt\(["']([a-z0-9_]+)["']""",
                                                    (root / "static/js" / script).read_text())}
    table = catalogs()
    used.discard("personality.json")  # a download file name, not a key
    missing = sorted(key for key in used if key not in table["en"] and not key.endswith("_"))
    assert not missing, missing
    ours = {key for key in table["en"] if key.startswith(("personality.", "js.personality_"))}
    assert ours == {key for key in table["it"] if key.startswith(("personality.", "js.personality_"))}
