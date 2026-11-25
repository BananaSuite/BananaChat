"""Every page that saves settings has one Save, at the top right of its page header (which stays in view while
the page scrolls), and no other: no Save at the bottom of the page or inside a card, and never several. Pages
that only list things (with their own actions: add, remove, revoke, test...) have none."""

from __future__ import annotations

import re

import pytest

from tests.app.test_model_lifecycle import sync

# A submit button (a <button> is one unless its type says otherwise) or a submit input, with its label.
BUTTON = re.compile(r"<button\b(?P<attrs>[^>]*)>(?P<label>.*?)</button>|<input\b(?P<input>[^>]*\btype=\"submit\"[^>]*)>",
                    re.S)
SAVE_WORDS = re.compile(r"\b(save|salva|speichern)\b", re.I)
SAVE_HEADER = re.compile(r'<header class="page-header page-header-save[^"]*">.*?</header>', re.S)


def save_controls(html: str) -> list[re.Match]:
    """The page's submit controls that save: labelled Save (in any of the shipped languages) or marked as the
    page's Save."""
    found = []
    for match in BUTTON.finditer(html):
        attrs = match.group("attrs") if match.group("attrs") is not None else match.group("input")
        if re.search(r'\btype="(button|reset)"', attrs):
            continue
        label = re.sub(r"<[^>]+>", "", match.group("label") or "") or \
            (re.search(r'\bvalue="([^"]*)"', attrs) or [None, ""])[1]
        if "data-page-save" in attrs or SAVE_WORDS.search(label):
            found.append(match)
    return found


def assert_one_save_in_the_header(html: str, page: str) -> None:
    controls = save_controls(html)
    assert len(controls) == 1, f"{page}: {len(controls)} save controls: {[m.group(0)[:120] for m in controls]}"
    [control] = controls
    header = SAVE_HEADER.search(html)
    assert header, f"{page}: no page header with the Save"
    assert header.start() <= control.start() < header.end(), f"{page}: the Save is not in the page header"
    assert "data-page-save" in control.group(0), page
    form_id = re.search(r'\bform="([^"]+)"', control.group(0))
    assert form_id, f"{page}: the Save names no form"
    form = re.search(rf'<form\b[^>]*\bid="{re.escape(form_id.group(1))}"[^>]*>', html)
    assert form and 'method="post"' in form.group(0), f"{page}: the Save's form {form_id.group(1)} is missing"
    assert form.start() > header.end(), f"{page}: the form comes before its Save"
    # Nothing after the main content saves: the header is the only place.
    assert not save_controls(html[header.end():]), page


def assert_forms_not_nested(html: str, page: str) -> None:
    """Sections that share one form keep their own actions in forms of their own, never inside it (a browser
    would drop the inner form and its button would submit the outer one)."""
    depth = 0
    for tag in re.finditer(r"<(/?)form\b", html):
        depth += -1 if tag.group(1) else 1
        assert depth in (0, 1), f"{page}: a form inside a form at {tag.start()}"
    assert depth == 0, page


def assert_no_save(html: str, page: str) -> None:
    controls = save_controls(html)
    assert not controls, f"{page}: {[m.group(0)[:120] for m in controls]}"


@pytest.fixture
def site(app, make_user):
    """One of everything the settings pages edit."""
    from bananachat.db import catalog, external_providers, personalities
    from bananachat.db import subscription_pool as accounts
    from bananachat.db import limits, users
    from bananachat.services import model_lifecycle

    sync(app)
    user = make_user("page-user")
    with app.app_context():
        admin = users.get_by_username("admin")
        model = catalog.get_by_name("qwen3:4b")
        category = catalog.create_category("Everyday")
        featured = personalities.create(admin["id"], "Guide", "Explain kindly.", created_by=admin["id"],
                                        kind="featured")
        own = personalities.create(admin["id"], "Tutor", "Explain.", created_by=admin["id"])
        provider = external_providers.save(name="Gateway", base_url="https://llm.example.com/v1", protocol="openai",
                                           enabled=1)
        account = accounts.add_account("primary", window_limit=1000)
        limits.set_model_override(user["id"], model["id"], None, locked=True, window_tokens=40_000)
        deprecated = catalog.get_by_name("llama3.2:3b")
        model_lifecycle.deprecate(deprecated, None, note="Old")
    return {"user": user, "model": model, "category": category, "featured": featured, "own": own,
            "provider": provider, "account": account, "deprecated": deprecated}


def settings_pages(site) -> list[str]:
    user, model = site["user"]["id"], site["model"]["id"]
    return [
        "/admin/settings", "/admin/quotas", "/admin/quotas/effort", "/admin/quotas/tiers",
        f"/admin/quotas/models/{model}", f"/admin/users/{user}", f"/admin/users/{user}/limits",
        "/admin/agents", "/admin/music", "/admin/models/settings", f"/admin/models/{model}/edit",
        f"/admin/models/{site['deprecated']['id']}/edit",
        f"/admin/models/categories/{site['category']}/edit", "/admin/personalities/featured/new",
        f"/admin/personalities/{site['featured']}/edit", "/admin/access/agents/0", f"/admin/access/model/{model}",
        f"/admin/models/providers/{site['provider']}", f"/admin/models/claude/accounts/{site['account']}",
        "/personalities/new", f"/personalities/{site['own']}/edit",
    ]


# Pages that list things or act on them, without settings of their own to save.
LIST_PAGES = ["/admin/quotas/models", "/admin/quotas/grants", "/admin/quotas/requests", "/admin/models",
              "/admin/models/providers", "/admin/models/downloads", "/admin/users", "/admin/invites",
              "/admin/personalities", "/admin/workers", "/admin/", "/admin/migration", "/admin/users/hosting",
              "/personalities", "/developer", "/account", "/customize", "/easter-eggs"]


def test_every_settings_page_has_one_save_in_its_page_header(app, admin, site):
    for page in settings_pages(site):
        response = admin.get(page)
        assert response.status_code == 200, page
        assert_one_save_in_the_header(response.get_data(as_text=True), page)
        assert_forms_not_nested(response.get_data(as_text=True), page)


def test_pages_without_settings_have_no_save(app, admin, site):
    for page in LIST_PAGES:
        response = admin.get(page)
        assert response.status_code == 200, page
        assert_no_save(response.get_data(as_text=True), page)
        assert_forms_not_nested(response.get_data(as_text=True), page)


def test_a_person_sees_one_save_on_their_own_pages(app, site, browser_for):
    from bananachat.db import access

    browser = browser_for()
    browser.login("page-user")
    with app.app_context():
        access.set_policy("custom_personality", 0, "allow_all", False, None)
    assert_one_save_in_the_header(browser.get("/personalities/new").get_data(as_text=True), "/personalities/new")
    for page in ("/personalities", "/account", "/customize", "/developer"):
        response = browser.get(page)
        assert response.status_code == 200, page
        assert_no_save(response.get_data(as_text=True), page)


def test_the_save_header_is_the_settings_pages_sticky_header():
    from pathlib import Path

    css = (Path(__file__).parents[2] / "bananachat/static/css/app.css").read_text()
    rule = re.search(r"\.page-header-save \{([^}]*)\}", css).group(1)
    assert "position: sticky" in rule and "top: var(--topbar-height)" in rule and "background: var(--bg)" in rule
