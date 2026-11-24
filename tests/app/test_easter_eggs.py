"""Easter eggs: the mascot's animations and its sunglasses, and the keyboard eggs an administrator designs
(BananaSuite banana-eggs/v1: the default one is the Konami code's shower). Site and personal switches, the
egg list in every page's head, the find endpoint, the easter egg page and the admin editor."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from tests.app.browser_helpers import chromium_executable
from tests.app.fixtures import Browser

ROOT = Path(__file__).resolve().parents[2]
KONAMI = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight", "b", "a"]
PARTY = {"id": "party", "keys": ["p", "a", "r", "t", "y"], "sprite": {"type": "emoji", "value": "🎉"},
         "unlock": {"action": "toast", "delay": 500}}


def _mascot(html: str) -> dict:
    tag = re.search(r"<button[^>]*data-mascot[^>]*>", html).group(0)
    return {name: value for name, value in re.findall(r'data-mascot-(motion|shades)="(\d)"', tag)}


def _eggs(html: str) -> dict | None:
    """The page's egg list: only from the head, as banana-eggs.js reads it."""
    head = html.split("</head>", 1)[0]
    match = re.search(r'<script type="application/json" id="banana-eggs">(.*?)</script>', head, re.S)
    assert html.count('id="banana-eggs"') == (1 if match else 0)
    return json.loads(match.group(1)) if match else None


def _signed_in(app, username):
    browser = Browser(app)
    browser.login(username)
    return browser


def _settings(app, **values):
    from bananachat.db import settings as site_settings

    with app.app_context():
        site_settings.update(**values)


def _store(app, egg_list):
    from bananachat import eggs

    _settings(app, easter_egg_configs=eggs.dumps(eggs.clean(egg_list, strict=True)))


def _settings_form(**extra):
    from bananachat.db import settings as site_settings

    form = {"site_name": "Test Chat", "signup_mode": "invite", "default_theme_mode": "dark",
            "easter_eggs_present": "1", "easter_mascot_motion": "on", **extra}
    for prefix, palette in (("", site_settings.DARK_PALETTE), ("light_", site_settings.LIGHT_PALETTE)):
        form.update({f"{prefix}{key}_color": value for key, value in palette.items()})
    return form


def _query(app, sql, params=()):
    from bananachat import db

    with app.app_context():
        return [tuple(row.to_dict().values()) for row in db.query(sql, params)]


# ----- switches and the page config ---------------------------------------------------------------------------

def test_every_easter_egg_is_on_by_default(app, make_user):
    make_user("ann")
    page = _signed_in(app, "ann").get("/customize").get_data(as_text=True)
    assert _mascot(page) == {"motion": "1", "shades": "1"}
    config = _eggs(page)
    assert [egg["id"] for egg in config["eggs"]] == ["konami"]
    assert config["eggs"][0]["keys"] == KONAMI and config["eggs"][0]["sprite"] == {"type": "mascot"}
    assert config["signedIn"] is True and config["reduceMotion"] is False
    assert config["foundUrl"] == "/api/easter-eggs/{id}/found" and config["pageUrl"] == "/easter-eggs"
    assert config["csrf"]["header"] == "X-CSRF-Token" and config["csrf"]["token"]
    assert "badge" not in config["eggs"][0]
    assert re.search(r'<script src="/static/[^"]*/banana-eggs/banana-eggs\.js" defer>', page.split("</head>")[0])
    assert re.search(r'<link rel="stylesheet" href="/static/[^"]*/banana-eggs/banana-eggs\.css">', page)
    # The mascot no longer handles the Konami code itself: the shared runtime does.
    mascot = (ROOT / "bananachat" / "static" / "js" / "mascot.js").read_text(encoding="utf-8")
    assert "ArrowUp" not in mascot and "keydown" not in mascot


def test_visitors_get_the_eggs_without_a_token(client):
    config = _eggs(client.get("/login").get_data(as_text=True))
    assert config["signedIn"] is False and config["csrf"] is None and config["eggs"]


def test_the_site_and_each_person_switch_them_off(app, make_user):
    from bananachat.i18n import translate

    make_user("ben")
    browser = _signed_in(app, "ben")
    saved = browser.post_json("/api/preferences", {"egg_mascot_shades": False}).get_json()["preferences"]
    assert saved["egg_mascot_shades"] is False and saved["egg_konami"] is True
    page = browser.get("/customize").get_data(as_text=True)
    assert _mascot(page) == {"motion": "1", "shades": "0"} and _eggs(page) is not None
    # The person's Konami switch now turns every keyboard egg off: no egg list at all.
    browser.post_json("/api/preferences", {"egg_konami": False})
    assert _eggs(browser.get("/customize").get_data(as_text=True)) is None
    browser.post_json("/api/preferences", {"egg_konami": True})
    _settings(app, easter_konami=0, easter_mascot_motion=0)
    page = browser.get("/customize").get_data(as_text=True)
    assert _mascot(page) == {"motion": "0", "shades": "0"} and _eggs(page) is None
    # What the site turned off cannot be switched on in Customise, and says why.
    language = re.search(r'<html lang="([a-z]+)"', page).group(1)
    assert re.search(r'<input type="checkbox" name="egg_konami"[^>]*disabled', page)
    assert translate(language, "customize.egg_site_off") in page


def test_less_motion_and_the_stored_list_reach_the_page(app, make_user):
    make_user("cora")
    browser = _signed_in(app, "cora")
    browser.post_json("/api/preferences", {"reduce_motion": True})
    _store(app, [{**PARTY, "badge": "secret"}, {"id": "off", "enabled": False, "keys": ["o", "f"]}])
    config = _eggs(browser.get("/customize").get_data(as_text=True))
    assert config["reduceMotion"] is True
    assert [egg["id"] for egg in config["eggs"]] == ["party"]  # enabled eggs only, without their badge
    assert "badge" not in config["eggs"][0] and config["eggs"][0]["sprite"]["value"] == "🎉"
    # No enabled egg: no list either.
    _store(app, [{"id": "off", "enabled": False, "keys": ["o", "f"]}])
    assert _eggs(browser.get("/customize").get_data(as_text=True)) is None


def test_the_egg_list_cannot_break_out_of_its_script(app, make_user):
    make_user("dan")
    _store(app, [{**PARTY, "message": "</script><script>alert(1)</script>"}])
    page = _signed_in(app, "dan").get("/customize").get_data(as_text=True)
    assert "<script>alert(1)" not in page
    assert _eggs(page)["eggs"][0]["message"] == "</script><script>alert(1)</script>"


# ----- finds ----------------------------------------------------------------------------------------------------

def test_a_find_is_recorded_once(app, make_user):
    ann = make_user("ann")
    browser = _signed_in(app, "ann")
    assert browser.post_json("/api/easter-eggs/konami/found").get_json() == {"ok": True, "new": True}
    assert browser.post_json("/api/easter-eggs/konami/found").get_json() == {"ok": True, "new": False}
    assert [row[0] for row in _query(app, "SELECT egg_id FROM easter_egg_finds WHERE user_id=?", (ann["id"],))] \
        == ["konami"]
    # Only the first find is logged.
    assert _query(app, "SELECT target FROM audit_log WHERE action='easter_egg.found'") == [("konami",)]


def test_finding_needs_an_account_and_the_csrf_token(app, make_user, client):
    response = client.post_json("/api/easter-eggs/konami/found")
    assert response.status_code == 401
    make_user("ben")
    browser = _signed_in(app, "ben")
    response = browser.client.post("/api/easter-eggs/konami/found", json={},
                                   headers={"Accept": "application/json", "X-CSRF-Token": "wrong"})
    assert response.status_code == 400
    assert browser.client.get("/api/easter-eggs/konami/found").status_code == 405
    assert _query(app, "SELECT COUNT(*) FROM easter_egg_finds") == [(0,)]


def test_only_eggs_on_offer_can_be_found(app, make_user):
    make_user("cleo")
    browser = _signed_in(app, "cleo")
    _store(app, [PARTY, {"id": "hidden", "enabled": False, "keys": ["h", "i"]}])
    for egg_id in ("konami", "hidden", "nope", "PARTY"):  # gone with the stored list, disabled, unknown, not an id
        assert browser.post_json(f"/api/easter-eggs/{egg_id}/found").status_code == 404, egg_id
    browser.post_json("/api/preferences", {"egg_konami": False})
    assert browser.post_json("/api/easter-eggs/party/found").status_code == 404  # turned off by the person
    browser.post_json("/api/preferences", {"egg_konami": True})
    _settings(app, easter_konami=0)
    assert browser.post_json("/api/easter-eggs/party/found").status_code == 404  # the site's switch
    _settings(app, easter_konami=1)
    assert browser.post_json("/api/easter-eggs/party/found").get_json() == {"ok": True, "new": True}


def test_finds_are_rate_limited_per_account(app, make_user):
    make_user("dora")
    make_user("emil")
    dora, emil = _signed_in(app, "dora"), _signed_in(app, "emil")
    answers = [dora.post_json("/api/easter-eggs/konami/found").status_code for _ in range(11)]
    assert answers == [200] * 10 + [429]
    assert emil.post_json("/api/easter-eggs/konami/found").status_code == 200


def test_the_easter_egg_page_lists_finds(app, make_user, client):
    from bananachat.i18n import translate
    from bananachat.services import exports

    fay = make_user("fay")
    _store(app, [{**PARTY, "unlock": {"action": "page", "delay": 500}}, {"id": "zebra", "keys": ["z", "z"]},
                 {"id": "hidden", "enabled": False, "keys": ["h", "i"]}])
    browser = _signed_in(app, "fay")
    browser.post_json("/api/easter-eggs/party/found")
    page = browser.get("/easter-eggs").get_data(as_text=True)
    language = re.search(r'<html lang="([a-z]+)"', page).group(1)
    main = page.split("<main", 1)[1].split("</main>", 1)[0]
    assert translate(language, "eggs.count", found=1, total=2) in main
    assert 'id="egg-party"' in main and "🎉" in main
    assert "zebra" not in main and "egg-hidden" not in main  # not found yet ("?"), not on offer
    assert main.count(translate(language, "eggs.not_found")) == 1
    assert 'href="/easter-eggs"' in browser.get("/customize").get_data(as_text=True)
    with app.app_context():
        assert [row["egg_id"] for row in exports.gdpr_export(fay["id"])["easter_egg_finds"]] == ["party"]
    # Visitors see the eggs as "?" and how to keep their finds.
    visitor = client.get("/easter-eggs")
    assert visitor.status_code == 200 and 'href="/login?next=/easter-eggs"' in visitor.get_data(as_text=True)


# ----- administration -------------------------------------------------------------------------------------------

def test_administrators_choose_the_easter_eggs_the_site_offers(app):
    from bananachat.db import settings as site_settings

    admin = _signed_in(app, "admin")
    page = admin.get("/admin/settings").get_data(as_text=True)
    assert 'name="easter_konami"' in page and "data-banana-eggs-editor" in page
    assert re.search(r'src="/static/[^"]*/banana-eggs/banana-eggs-admin\.js"', page)
    options = json.loads(re.search(r"data-banana-eggs-editor-options>(.*?)</script>", page, re.S).group(1))
    assert options["badges"] == [] and options["buttonClass"] == "btn btn-sm" and options["egg"]["id"] == "konami"
    assert admin.post("/admin/settings", _settings_form()).status_code == 302
    with app.app_context():
        values = site_settings.get()
    assert (values["easter_mascot_motion"], values["easter_mascot_shades"], values["easter_konami"]) == (1, 0, 0)
    assert values["easter_egg_configs"] is None  # the editor did not post: the default egg stays
    # A cached form without the easter egg fields changes none of them.
    form = _settings_form(easter_konami="on")
    form.pop("easter_eggs_present")
    admin.post("/admin/settings", form)
    with app.app_context():
        assert site_settings.get()["easter_konami"] == 0


def test_administrators_save_the_egg_list(app):
    from bananachat import eggs
    from bananachat.db import settings as site_settings

    admin = _signed_in(app, "admin")
    posted = {"version": 1, "eggs": [eggs.default_egg(), {**PARTY, "message": "  Party   time ", "keys": ["P", "A"]}]}
    response = admin.post("/admin/settings", _settings_form(easter_egg_configs=json.dumps(posted)))
    assert response.status_code == 302
    with app.app_context():
        stored = json.loads(site_settings.get()["easter_egg_configs"])
    assert [egg["id"] for egg in stored["eggs"]] == ["konami", "party"]
    assert stored["eggs"][1]["keys"] == ["p", "a"] and stored["eggs"][1]["message"] == "Party time"
    assert _query(app, "SELECT details FROM audit_log WHERE action='admin.settings_save'")[-1][0] \
        .count('"easter_egg_configs": ["konami", "party"]') == 1
    page = admin.get("/admin/settings").get_data(as_text=True)
    assert "party" in re.search(r'id="easter-egg-configs"[^>]*value="([^"]*)"', page).group(1)


def test_a_wrong_egg_list_is_refused_with_its_problems(app):
    from bananachat.db import settings as site_settings

    admin = _signed_in(app, "admin")
    posted = {"version": 1, "eggs": [{**PARTY, "count": 500}, {**PARTY}, {"id": "Bad id", "keys": ["a", "b"]},
                                     {"id": "short", "keys": ["x"]}]}
    response = admin.post("/admin/settings", _settings_form(site_name="Renamed", easter_egg_configs=json.dumps(posted)))
    assert response.status_code == 400
    page = response.get_data(as_text=True)
    for problem in ("Easter egg 1, how many: must be between 1 and 200.",
                    "Easter egg 2, name: is used by another easter egg.",
                    "Easter egg 3, name: must be 1 to 32 lower-case letters, digits or hyphens.",
                    "Easter egg 4, key sequence: needs 2 to 32 keys."):
        assert problem in page, problem
    assert "Nothing was saved." in page
    # The administrator's edits come back for another try, and nothing was stored.
    assert 'value="Renamed"' in page and "Bad id" in page
    with app.app_context():
        values = site_settings.get()
    assert values["site_name"] == "Test Chat" and values["easter_egg_configs"] is None
    assert admin.post("/admin/settings", _settings_form(easter_egg_configs="{not json")).status_code == 400


def test_only_administrators_change_the_egg_list(app, make_user):
    from bananachat.db import settings as site_settings

    make_user("gus")
    user = _signed_in(app, "gus")
    form = _settings_form(easter_egg_configs=json.dumps([PARTY]))
    assert user.post("/admin/settings", form).status_code == 403
    response = user.client.post("/admin/settings", data={**form, "csrf_token": "wrong"})
    assert response.status_code in (400, 403)
    with app.app_context():
        assert site_settings.get()["easter_egg_configs"] is None


# ----- in the browser -------------------------------------------------------------------------------------------

@pytest.fixture
def customise_page(app, make_user):
    sync_api = pytest.importorskip("playwright.sync_api")
    from werkzeug.serving import make_server

    make_user("cleo")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_api.sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(executable_path=chromium_executable())
            except Exception as error:  # noqa: BLE001 - optional browser dependency
                pytest.skip(f"Chromium is not available: {error}")
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/login")
            page.fill("#username", "cleo")
            page.fill("#password", "cleo-password")
            page.wait_for_timeout(600)
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            page.goto(base + "/customize")
            page.wait_for_selector("#easter-eggs")
            yield page
            browser.close()
            assert errors == []
    finally:
        server.shutdown()


def _konami(page):
    page.locator("h1").click()  # focus the page, not a form field
    for key in KONAMI:
        page.keyboard.press(key)


def test_easter_eggs_switch_off_in_the_browser(customise_page):
    page = customise_page
    with page.expect_response("**/api/easter-eggs/konami/found") as found:
        _konami(page)
    assert found.value.json() == {"ok": True, "new": True}
    page.wait_for_selector(".banana-eggs-item", state="attached", timeout=3000)
    for _ in range(11):
        page.click("[data-mascot]")
    assert page.evaluate("() => document.querySelector('.mascot-sprite').classList.contains('mascot-sprite--shades')")

    # Off in Customise: the sunglasses come off at once; the keyboard eggs from the next page on.
    page.locator('label:has(input[name="egg_konami"])').click()
    page.locator('label:has(input[name="egg_mascot_shades"])').click()
    page.wait_for_function("() => !document.querySelector('.mascot-sprite').classList.contains('mascot-sprite--shades')")
    page.wait_for_selector("#save-status.is-saved", timeout=5000)
    page.reload()
    page.wait_for_selector("#easter-eggs")
    assert page.locator("head script#banana-eggs").count() == 0
    page.wait_for_selector(".banana-eggs-item", state="detached", timeout=8000)
    _konami(page)
    page.wait_for_timeout(500)
    assert page.locator(".banana-eggs-item").count() == 0
    for _ in range(11):
        page.click("[data-mascot]")
    assert not page.evaluate(
        "() => document.querySelector('.mascot-sprite').classList.contains('mascot-sprite--shades')")
