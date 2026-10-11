"""Regressions from the 1.7.0 visual review: auth messages, chat header and shared view, copy, layout contracts."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.app.conftest import Browser
from tests.app.test_chat_pages import alice, chat_with_answer  # noqa: F401  (fixture)
from tests.app.test_limits_tokens import _ensure_column, _models, _signed_in

ROOT = Path(__file__).resolve().parents[2]
CSS = ROOT / "bananachat" / "static" / "css"


def _css(name: str) -> str:
    return (CSS / name).read_text()


def _rule(css: str, selector: str) -> str:
    """The declarations of the first rule whose selector list is exactly *selector*."""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, selector
    return match.group(1)


# ----- sign-in messages ----------------------------------------------------------------

def test_a_failed_sign_in_explains_itself_next_to_the_form(client):
    response = client.post("/login", {"username": "admin", "password": "wrong-password"})
    html = response.get_data(as_text=True)
    assert response.status_code == 401
    # Inline, announced, and read with the field the person has to fill in again.
    assert re.search(r'<div class="auth-messages[^"]*" id="auth-messages">\s*<div class="alert alert-error" role="alert">', html)
    assert re.search(r'<input type="password" id="password"[^>]*autofocus aria-describedby="auth-messages"', html)
    assert not re.search(r'id="username"[^>]*autofocus', html)
    assert "data-flash" not in html  # not also as a toast in the corner


def test_auth_pages_show_the_current_language_and_the_common_footer(client):
    html = client.get("/login", headers={"Accept-Language": "en"}).get_data(as_text=True)
    assert 'aria-current="true">English</a>' in html
    assert 'class="site-footer"' in html and "AGPL-3.0" in html
    assert re.search(r'\.auth-footer a\[aria-current="true"\] \{[^}]*text-decoration: none', _css("app.css"))


# ----- chat ------------------------------------------------------------------------------

def test_the_shared_view_calls_the_sharer_user_and_uses_one_column(app, alice):  # noqa: F811
    session_id = chat_with_answer(alice)
    url = alice.post_json(f"/chat/{session_id}/share", {"action": "create"}).json["url"]
    html = Browser(app).get(url.replace("http://localhost", ""), headers={"Accept-Language": "en"}).get_data(as_text=True)
    assert 'aria-label="User"' in html and 'aria-label="You"' not in html
    assert '<div class="page page-narrow">' in html


def test_a_no_history_chat_header_fits_a_phone(app, alice):  # noqa: F811
    session_id = chat_with_answer(alice, incognito=True)
    html = alice.get(f"/chat/{session_id}").get_data(as_text=True)
    assert '<span class="chat-badge-label">' in html
    assert re.search(r'class="btn btn-sm chat-header-button" aria-label="[^"]+"[^>]*>.*?<span class="chat-action-label">', html, re.S)
    phone = _css("chat.css").split("@media (max-width: 640px)")[-1]
    assert ".chat-action-label { display: none; }" in phone


def test_the_chat_page_keeps_the_top_bar_height():
    css = _css("chat.css")
    assert "flex: none" in _rule(css, "body.chat-page > .topbar, body.chat-page > .status-region")
    assert "flex: 1 1 0" in _rule(css, "body.chat-page > main")


def test_attachments_do_not_stretch_and_images_are_bounded():
    css = _css("chat.css")
    assert "align-items: flex-start" in _rule(css, ".attachment-list")
    assert "max-width: min(240px, 100%)" in _rule(css, ".attachment-image")


# ----- copy and formatting ------------------------------------------------------------------

def test_image_cost_names_the_token_unit_once(app):
    from bananachat.formatting import tokens_text
    from bananachat.i18n import translate

    for language, unit in (("en", "tokens tokens"), ("it", "token token")):
        text = translate(language, "images.cost", cost=tokens_text(5000, language), left=tokens_text(45000, language))
        assert unit not in text, text


def test_time_left_leaves_out_a_zero_second_unit():
    from bananachat.formatting import time_left

    now = datetime.now(timezone.utc)
    assert time_left(now + timedelta(hours=5, seconds=-10), "it") == "5 h"
    assert time_left(now + timedelta(days=7, seconds=-10), "it") == "7 g"
    assert time_left(now + timedelta(days=7, seconds=-10), "en") == "7 d"
    assert time_left(now + timedelta(hours=2, minutes=5, seconds=-10), "en") == "2 h 5 min"


def test_administrator_pages_format_times_and_numbers_in_english(app):
    from flask import g

    from bananachat.formatting import number, timeago

    earlier = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    with app.test_request_context("/admin/users"):
        g.lang = "it"
        assert timeago(earlier).endswith("ago") and number(12345) == "12,345"
    with app.test_request_context("/account"):
        g.lang = "it"
        assert timeago(earlier).endswith("fa") and number(12345) == "12\u202f345"


def test_json_in_the_agent_log_shows_real_characters(app):
    with app.test_request_context():
        html = app.jinja_env.from_string("<pre>{{ value | pretty_json }}</pre>").render(
            value={"command": "mkdir -p src && echo 'hi'"})
    assert "&amp;&amp;" in html and "&#39;hi&#39;" in html and "\\u0026" not in html


def test_dates_in_page_text_do_not_break_inside(app):
    with app.test_request_context():
        html = str(app.jinja_env.from_string("{{ value | datetime_html }}").render(value="2026-10-09 15:14:00"))
    assert html == '<span class="nowrap">2026-10-09</span> <span class="nowrap">15:14 UTC</span>'


def test_off_on_reasoning_reads_as_words_on_the_account_page(app, make_user):
    from bananachat import db

    models = _models(app)
    make_user("ivo")
    with app.app_context():
        _ensure_column("ai_models", "reasoning_levels", "TEXT")
        db.execute("UPDATE ai_models SET is_reasoning=1, reasoning_levels='[\"off\",\"on\"]' WHERE id=?",
                   (models["qwen3:4b"]["id"],))
    for language, words in (("en", ("reasoning on", "Reasoning is off")), ("it", ("attivare il ragionamento",
                                                                                   "ragionamento è disattivato"))):
        page = _signed_in(app, "ivo", language=language).get("/account").get_data(as_text=True)
        assert any(text in page for text in words), language
        assert "Ask for On" not in page and "Chiedi Attivo" not in page


def test_the_amount_hint_sits_with_the_token_fields(app, make_user):
    make_user("una")
    page = _signed_in(app, "una").get("/account").get_data(as_text=True)
    assert re.search(r'id="quota_tokens"[^>]*aria-describedby="quota_tokens_hint"', page)
    assert 'id="quota_tokens_hint"' in page and 'id="quota_extra_hint"' in page


def test_the_administration_is_marked_english_for_italian_administrators(app):
    html = _signed_in(app, "admin", language="it").get("/admin/").get_data(as_text=True)
    assert '<div class="admin-shell" lang="en">' in html


# ----- shared style contracts ------------------------------------------------------------------

def _luminance(rgb):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first, second):
    a, b = _luminance(first), _luminance(second)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _hex(colour):
    return tuple(int(colour[index:index + 2], 16) for index in (1, 3, 5))


def _mix(first, second, share):
    return tuple(a * share + b * (1 - share) for a, b in zip(first, second, strict=True))


def test_primary_text_on_tinted_backgrounds_meets_aa_in_the_light_theme():
    from bananachat.db.settings import LIGHT_PALETTE

    css = _css("app.css")
    light = css.split(':root[data-theme="light"]')[1].split("}")[0]
    share = int(re.search(r"--primary-text: color-mix\(in srgb, var\(--primary\) (\d+)%, var\(--text\)\)", light).group(1))
    primary, text = _hex(LIGHT_PALETTE["primary"]), _hex(LIGHT_PALETTE["text"])
    colour = _mix(primary, text, share / 100)
    info = _hex(re.search(r"--info: (#[0-9a-f]{6})", light).group(1))
    warning = _hex(re.search(r"--warning: (#[0-9a-f]{6})", light).group(1))
    backgrounds = {
        "badge on the page": _mix(primary, _hex(LIGHT_PALETTE["bg"]), 0.10),
        "badge on a card": _mix(primary, _hex(LIGHT_PALETTE["sidebar"]), 0.10),
        "info alert": _mix(info, _hex(LIGHT_PALETTE["bg"]), 0.14),
        "warning alert": _mix(warning, _hex(LIGHT_PALETTE["bg"]), 0.14),
    }
    for name, background in backgrounds.items():
        assert _contrast(colour, background) >= 4.5, name
    assert "var(--primary-text)" in _rule(css, ".badge-primary")
    assert "var(--primary-text)" in _rule(css, ".alert a")


def test_table_and_component_rules_that_were_overridden():
    css = _css("app.css")
    # A right-aligned numeric column wins over the table's text-align: left.
    assert "text-align: right" in _rule(css, ".table :is(th, td).text-right")
    # Row headers of the last row lose their border too.
    assert "border-bottom: 0" in _rule(css, ".table tbody tr:last-child > :is(th, td)")
    # One raised chip per selected segment (the inner span only as a fallback without :has()).
    assert ".segmented input:checked + span" in css.split("@supports not selector(:has(*))")[1].split("}")[0]
    assert "inset 0 0 0 1px" in _rule(css, '.segmented button[aria-pressed="true"], .segmented label:has(input:checked)')
    # The letter-spacing preference reaches form controls; the off state of a switch is visible.
    assert "letter-spacing: inherit" in _rule(css, "input, select, textarea, button")
    assert "var(--control-off)" in _rule(css, ".switch .track")
    # Tabs wrap on phones instead of hiding the current tab off-screen.
    assert re.search(r"@media \(max-width: 640px\) \{\s*\.tabs \{ flex-wrap: wrap;", css)


def test_the_personality_editor_shows_the_preview_before_the_buttons_on_narrow_screens():
    css = _css("personalities.css")
    narrow = css.split("@media (max-width: 900px)")[1]
    assert ".persona-editor > .persona-form { display: contents; }" in narrow
    assert ".persona-form > .form-actions { order: 4; }" in narrow
    assert "grid-template-columns: minmax(0, 1fr)" in _rule(css, ".starter-fields")


def test_service_notices_stay_short_above_a_conversation_on_phones():
    phone = _css("chat.css").split("@media (max-width: 640px)")[-1]
    assert "body.chat-page .status-banner.is-blocking .status-text { display: none; }" in phone


def test_each_language_uses_one_quotation_style():
    import json

    for path in sorted((ROOT / "bananachat" / "i18n").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))

        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)

        for language, wrong in (("en", "«»’"), ("it", "“”’")):
            for text in strings(data.get(language, {})):
                assert not any(mark in text for mark in wrong), (path.name, language, text)


def test_italian_reset_times_read_well_with_a_date_or_a_time():
    """{time}/{date} from time_text is "14:05 UTC" today but "2026-10-10 01:12 UTC" otherwise: no "alle" before it."""
    import json

    for path in sorted((ROOT / "bananachat" / "i18n").glob("*.json")):
        italian = json.loads(path.read_text(encoding="utf-8")).get("it", {})
        for key, text in italian.items():
            if isinstance(text, str) and key != "js.agents.workspace_until":  # a clock time only (agent-task.js)
                assert not re.search(r"\balle \{(time|date)\}", text), (path.name, key, text)


def test_preference_options_never_leave_one_option_alone_and_the_site_name_uses_the_free_space():
    css = _css("customization.css")
    assert "flex: 1 1 calc((19rem - 100%) * 999)" in _rule(css, ".choice-group .segmented:not(:has(> label:nth-child(6))) > label")
    assert "max-width: none" in _rule(_css("app.css"), ".topbar.nav-collapsed .brand span")


def test_the_limits_of_an_account_list_each_value_under_its_name(app, make_user):
    make_user("vera")
    with app.app_context():
        from bananachat.db import users
        user_id = users.get_by_username("vera")["id"]
    html = _signed_in(app, "admin").get(f"/admin/users/{user_id}").get_data(as_text=True)
    assert '<dl class="details details-stacked">' in html
    assert "grid-template-columns: minmax(0, 1fr)" in _rule(_css("app.css"), "dl.details.details-stacked")


def test_moderation_and_catalog_tables_become_cards_on_phones(app):
    from bananachat.db import personalities, users

    _models(app)
    with app.app_context():
        admin_id = users.get_by_username("admin")["id"]
        personalities.create(admin_id, "Guide", "Be helpful.", created_by=admin_id, kind="featured")
    client = _signed_in(app, "admin")
    assert 'class="table table-stack table-stack-md admin-persona-table"' in client.get("/admin/personalities").get_data(as_text=True)
    assert 'class="table table-stack catalog-table"' in client.get("/admin/models").get_data(as_text=True)
    assert "display: none" in _rule(_css("admin.css"), ".catalog-table.table-stack .col-optional")


def test_reset_times_in_sentences_do_not_break_inside_and_stay_escaped(app):
    from markupsafe import Markup

    from bananachat.formatting import time_html
    from bananachat.i18n import translate_html

    later = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    with app.test_request_context():
        moment = time_html(later)
        assert re.fullmatch(r'<span class="nowrap">\d{4}-\d\d-\d\d</span> <span class="nowrap">\d\d:\d\d UTC</span>', moment)
        html = translate_html("it", "account.window_resets", time=moment, left="<b>3 g</b>")
    assert isinstance(html, Markup) and str(moment) in html and "&lt;b&gt;3 g&lt;/b&gt;" in html


# ----- 1.7.0 visual recheck ----------------------------------------------------------------

def test_a_failed_sign_up_focuses_and_marks_the_field_the_message_is_about(app):
    form = {"username": "newcomer", "password": "long-password", "confirm_password": "long-password",
            "invite_code": "NOPE-NOPE-NOPE"}
    html = Browser(app).post("/signup", form).get_data(as_text=True)
    assert re.search(r'id="invite_code"[^>]*autofocus aria-describedby="auth-messages" aria-invalid="true"', html)
    assert not re.search(r'id="username"[^>]*autofocus', html)
    mismatch = Browser(app).post("/signup", {**form, "confirm_password": "something-else"}).get_data(as_text=True)
    assert re.search(r'id="password"[^>]*aria-describedby="auth-messages password-hint" autofocus aria-invalid="true"',
                     mismatch)
    assert not re.search(r'id="invite_code"[^>]*autofocus', mismatch)


def test_the_setup_hint_shows_the_command_name_as_code(app):
    from markupsafe import Markup

    from bananachat.i18n import translate_html

    code = Markup('<code translate="no">banana</code>')
    for language in ("en", "it"):
        assert str(code) in translate_html(language, "auth.setup_token_hint_command", command=code)


def test_the_public_language_switch_names_each_language(app):
    html = Browser(app).get("/no-such-page", headers={"Accept-Language": "en"}).get_data(as_text=True)
    assert '<span aria-hidden="true">IT</span><span class="visually-hidden">Italiano</span>' in html


def test_an_off_on_reasoning_request_reads_as_reasoning_on(app, admin, make_user):
    from bananachat import db

    from bananachat.db import limits as store

    models = _models(app)
    user = make_user("oona")
    with app.app_context():
        _ensure_column("ai_models", "reasoning_levels", "TEXT")
        db.execute("UPDATE ai_models SET is_reasoning=1, reasoning_levels='[\"off\",\"on\"]' WHERE id=?",
                   (models["qwen3:4b"]["id"],))
        store.set_effort_level(user["id"], None, "low")  # "On" is open by default; lock it for this account.
    browser = _signed_in(app, "oona")
    form = browser.get("/account?request=effort&model=qwen3:4b#quota").get_data(as_text=True)
    assert re.search(r'<label for="quota_effort_level"[^>]*>Reasoning</label>', form)
    response = browser.post("/account/quota-request", {"kind": "effort", "effort_model": "qwen3:4b",
                                                       "effort_level": "medium", "reason": "Long proofs"})
    assert response.status_code == 302
    page = browser.get("/account").get_data(as_text=True)
    assert "Reasoning on · " in page and "Reasoning up to Medium" not in page
    assert "Reasoning on · " in admin.get("/admin/quotas/requests").get_data(as_text=True)


def test_extra_tokens_name_their_period_once(app):
    from flask import g

    from bananachat.web.account import describe_grant

    grant = {"kind": "extra", "amount": 50000, "scope": "window", "model_id": None, "pool": "chat"}
    with app.test_request_context():
        g.lang = "en"
        assert describe_grant(grant) == "+50k tokens per 5 hours · Chat"
        g.lang = "it"
        assert describe_grant(grant).startswith("+50k token ogni 5 ore · ")


def test_action_codes_break_only_between_their_parts(app):
    from bananachat.formatting import break_points

    assert str(break_points("account.two_step_enable")) == "account.<wbr>two_<wbr>step_<wbr>enable"
    assert "&lt;b&gt;" in str(break_points("<b>"))
    assert "overflow-wrap: anywhere" not in _rule(_css("admin.css"), ".break-code")


def test_the_composer_keeps_one_row_and_shows_the_model_name_when_there_is_room():
    css = _css("chat.css")
    phone = css.split("@media (max-width: 640px)")[-1]
    assert "max-width: 7.5em" not in css
    assert "#model-picker { flex: 1 1 6rem; min-width: 6rem; max-width: max-content; }" in phone
    assert "display: flex" in _rule(css, ".composer-tools > .model-picker")
    assert ".composer-tools:has(> #effort-picker:not([hidden])):has(> #persona-picker) .composer-action-label" in css
    assert "display: none" in _rule(css, ".composer-paused:not(:empty) + .composer-hint")


def test_the_history_drawer_is_visible_as_soon_as_it_opens():
    css = _css("chat.css")
    drawer = css.split("@media (max-width: 900px)")[1]
    assert "visibility 0s linear var(--speed)" in drawer
    assert "transition: transform var(--speed), visibility 0s;" in drawer
    script = (ROOT / "bananachat" / "static" / "js" / "chat.js").read_text()
    assert "setCovered(true)" in script and "sidebar.search.focus()" in script


def test_layout_rules_from_the_recheck():
    app_css, admin_css = _css("app.css"), _css("admin.css")
    # A lone dismiss button is centred on the notice's first line at every width.
    assert "align-self: flex-start" in _rule(app_css, ".status-actions:not(:has(> .btn))")
    # Wide tables become cards up to 900px; card cells are not capped or cut.
    assert "@media (max-width: 900px) {\n  .table.table-stack-md" in app_css
    assert "max-width: none" in _rule(app_css, ".table.table-stack td.cell-wide")
    # Account and customize share one section navigation (wrapping with large text).
    assert "flex-wrap: wrap" in app_css.split("@container (max-width: 18rem)")[1].split("}")[0]
    # The API key actions keep their names when only their icons show.
    assert "display: none" not in _css("developer.css").split(".dev-tokens .actions .btn > span")[1].split("}")[0]
    # Short vote labels and one-line session details.
    assert "white-space: nowrap" in _rule(_css("community.css"), ".community-vote .btn")
    assert "flex-wrap: nowrap" in _rule(_css("agents.css"), ".agents-task-meta")
    # The personality preview on narrow screens has one rule, under it.
    narrow = _css("personalities.css").split("@media (max-width: 900px)")[1]
    assert "border-bottom: 1px solid var(--border)" in narrow.split(".persona-preview-inner")[1].split("}")[0]
    # Catalog cards line every row up beside the order buttons; the admin page ends without a rule.
    assert "display: grid" in admin_css.split(".catalog-table.table-stack tr {")[1].split("}")[0]
    assert "border-bottom: 0" in _rule(admin_css, ".admin-main > .settings-section:last-child")


def test_admin_tables_from_the_recheck_become_cards(app, make_user):
    make_user("tess")
    _signed_in(app, "tess")  # one signed-in session to list
    client = _signed_in(app, "admin")
    with app.app_context():
        from bananachat.db import users
        user_id = users.get_by_username("tess")["id"]
    detail = client.get(f"/admin/users/{user_id}").get_data(as_text=True)
    assert '<table class="table table-stack">' in detail and 'data-label="Last active:"' in detail
    categories = (ROOT / "bananachat" / "templates" / "admin" / "categories.html").read_text()
    assert 'class="table table-stack catalog-table categories-table"' in categories
    # Both chat audit tabs keep one header, so the tab bar does not move.
    assert "<h1>Chat audit</h1>" in client.get("/admin/incognito").get_data(as_text=True)
    template = (ROOT / "bananachat" / "templates" / "admin" / "incognito_session.html").read_text()
    assert '<p class="breadcrumb"><a href="{{ url_for(\'admin.incognito\') }}">' in template


# ----- 1.7.0 visual recheck, second pass ---------------------------------------------------------

def test_phrases_with_a_number_and_a_duration_do_not_break_inside():
    import json

    catalog = json.loads((ROOT / "bananachat" / "i18n" / "account.json").read_text())
    assert catalog["en"]["account.rate_burst"].endswith("(bursts\xa0of\xa0up\xa0to\xa0{burst})")
    assert catalog["it"]["account.rate_burst"].endswith("(fino\xa0a\xa0{burst}\xa0di\xa0fila)")
    assert "(in\xa0{left})" in catalog["en"]["account.weekly_resets"]
    assert "tra\xa0{left}" in catalog["it"]["account.window_resets"]


def test_the_last_sign_in_time_wraps_between_date_and_time(app, make_user):
    make_user("lina")
    page = _signed_in(app, "lina").get("/account").get_data(as_text=True)
    assert '<span class="faint small last-sign-in-time">(<span class="nowrap">' in page
    assert "display: block" in _rule(_css("account.css"), ".account-profile-details .last-sign-in-time")


def test_the_api_address_wraps_and_key_actions_are_icons_at_every_width(app, make_user):
    make_user("theo")
    page = _signed_in(app, "theo").get("/developer").get_data(as_text=True)
    assert re.search(r'<code class="dev-endpoint-url" translate="no" title="[^"]+">http', page)
    css = _css("developer.css")
    assert "overflow-wrap: anywhere" in css.split(".dev-endpoint-url {")[1].split("}")[0]
    # Not inside a media query: the page column is as wide on every desktop.
    assert "\n.dev-tokens .actions .btn > span:not(.visually-hidden) { position: absolute;" in css


def test_requests_and_grants_read_well_on_phones_and_in_the_administration(app, admin, make_user):
    from tests.app.test_limits import _grant

    user = make_user("gwen")
    with app.app_context():
        _grant(user_id=user["id"], pool="chat", scope="window", kind="extra", amount=50_000)
    page = admin.get("/admin/quotas/grants").get_data(as_text=True)
    assert "<strong>+50k tokens per 5 hours</strong> · Chat" in page and "5-hour tokens ·" not in page
    template = (ROOT / "bananachat" / "templates" / "account" / "index.html").read_text()
    assert template.count('<table class="table table-stack">') == 2  # request and access histories


def test_the_short_description_grows_like_the_conversation_starters():
    for name in ("personalities/edit.html", "admin/personality_edit.html"):
        template = (ROOT / "bananachat" / "templates" / name).read_text()
        assert '<textarea class="starter-input" rows="1" id="description" name="description"' in template


def test_administration_tables_fit_tablets_and_small_laptops():
    app_css, admin_css = _css("app.css"), _css("admin.css")
    templates = ROOT / "bananachat" / "templates" / "admin"
    # Many-column tables are cards below 1280px, where the menu leaves a tablet-sized column.
    assert "@media (max-width: 1279px) {\n  .table.table-stack-lg" in app_css
    for name, marker in (("workers.html", 'class="table table-stack table-stack-lg" id="workers-table"'),
                         ("metrics.html", '<table class="table table-stack table-stack-lg">'),
                         ("users.html", '<th scope="col" class="col-optional col-lg">Created</th>'),
                         ("audit.html", '<th scope="col" class="col-lg">Address</th>'),
                         ("personalities.html", '<th scope="col" class="col-lg">Updated</th>'),
                         ("providers.html", 'class="table table-stack table-stack-md provider-model-table"'),
                         ("agents.html", 'data-label="Use for sessions:"'),
                         ("music.html", 'data-label="Participating:"')):
        assert marker in (templates / name).read_text(), name
    assert "@media (min-width: 641px) and (max-width: 1279px) {\n  .table:not(.table-stack-md, .table-stack-lg) .col-lg { display: none; }" in admin_css
    # A shortened device or label takes a share of the row instead of setting the table's width.
    assert "max-width: 0; width: 40%" in admin_css
    # The share bar never shows in stacked metric cards; one-line audit actions.
    assert ".table.table-stack .meter-col" in admin_css
    assert "white-space: nowrap" in _rule(admin_css, ".audit-entry-meta .btn-ghost.btn-sm")
    # Grids and request cards that end a page clip the rule under their last row.
    assert "overflow-y: clip" in admin_css and "margin-bottom: -1px" in admin_css


def test_a_request_opened_from_the_models_card_also_asks_the_community(app, make_user):
    from bananachat import db
    from bananachat.db import limits as store

    models = _models(app)
    user = make_user("nina")
    with app.app_context():
        _ensure_column("ai_models", "reasoning_levels", "TEXT")
        db.execute("UPDATE ai_models SET is_reasoning=1, reasoning_levels='[\"low\",\"medium\",\"high\",\"max\"]' "
                   "WHERE id=?", (models["qwen3:4b"]["id"],))
        store.set_effort_level(user["id"], None, "low")
    browser = _signed_in(app, "nina")
    box = re.compile(r'<input type="checkbox" name="community" value="1"( checked)?>')
    # The same default as the form on a plain /account load, where it is checked.
    plain = box.search(browser.get("/account").get_data(as_text=True))
    prefilled = box.search(browser.get("/account?request=effort&model=qwen3:4b&level=high#quota").get_data(as_text=True))
    assert plain and plain.group(1) and prefilled and prefilled.group(1)
    # A rejected form keeps what the person chose.
    response = browser.post("/account/quota-request", {"kind": "effort", "effort_model": "qwen3:4b",
                                                       "effort_level": "high", "reason": "x"})
    assert response.status_code == 400
    refused = box.search(response.get_data(as_text=True))
    assert refused and not refused.group(1)


def test_polling_downloads_keeps_the_reason_a_download_failed():
    source = (ROOT / "bananachat" / "static" / "js" / "admin.js").read_text()
    poll = source.split("function initDownloads()", 1)[1].split("\nfunction ", 1)[0]
    assert 'detail.textContent = (job.status === "failed" ? job.error : job.detail) || "";' in poll
    template = (ROOT / "bananachat" / "templates" / "admin" / "model_downloads.html").read_text()
    assert '{{ job.error_message if job.status == "failed" else job.progress_detail }}' in template


def test_personality_actions_wrap_as_one_spaced_group(app, admin, make_user):
    make_user("olga")
    admin.post("/admin/personalities/create", {"username": "olga", "name": "Poet", "instructions": "Rhyme."})
    page = admin.get("/admin/personalities").get_data(as_text=True)
    assert re.search(r'<td class="actions">\s*<div class="row-end admin-persona-actions">', page)
    assert "gap: .35rem" in _rule(_css("personalities.css"), ".row-end.admin-persona-actions")


def test_agent_messages_and_results_wrap_on_phones_and_only_arguments_scroll(app, make_user):
    from bananachat.db import agents as agents_db

    user = make_user("ada")
    with app.app_context():
        agents_db.create("task-wrap", user_id=user["id"], title="Tiny app",
                         prompt="Create a tiny Python app that prints hello robots and a test for it",
                         model_id=None, model_name="qwen3:4b", swarm=False, owner_token="t", max_user=None,
                         max_site=10)
        agents_db.add_step("task-wrap", None, kind="tool", tool_name="write_file",
                           tool_args={"path": "src/app.py", "content": "print('hello robots')\n"},
                           tool_result="Wrote 22 bytes to /workspace/src/app.py", tool_status="ok")
    admin = Browser(app)
    admin.login("admin", "admin-password")
    page = admin.get("/admin/agents/tasks/task-wrap").get_data(as_text=True)
    assert '<pre class="tool-pre">Create a tiny Python app' in page  # the user message
    assert '<div class="tool-label">Result</div><pre class="tool-pre">Wrote 22 bytes' in page
    assert re.search(r'<div class="tool-label">Arguments</div><pre class="tool-pre tool-pre-code">', page)
    css = _css("agents.css")
    phones = css.split("@media (max-width: 640px) {\n  .admin-step", 1)[1].split("\n}\n", 1)[0]
    assert phones.startswith(" .tool-pre-code {") and "white-space: pre;" in phones
    assert ".admin-step .tool-pre {" not in phones


def test_administration_tables_never_scroll_sideways_between_the_breakpoints():
    """Measured from 375 to 1440px in 20-50px steps: no table is wider than its box at any width."""
    app_css, admin_css = _css("app.css"), _css("admin.css")
    # The menu is a disclosure row below 1024px, where a sidebar would leave a phone-sized column ...
    assert re.search(r"@media \(max-width: 1023px\) \{\s*/\* The navigation row", admin_css)
    assert "@media (max-width: 900px) {\n  /* The navigation row" not in admin_css
    # ... and below 1280px the page column beside it is no wider than a tablet's.
    assert "1100px" not in admin_css and "1100px" not in _css("personalities.css")
    assert "@media (max-width: 1279px) {\n  .table .actions { white-space: normal; }" in admin_css
    # Cards up to 900px have the phone cards' rules for empty values and long cells.
    md = app_css.split("@media (max-width: 900px) {\n  .table.table-stack-md", 1)[1].split("\n}\n", 1)[0]
    assert "td:has(> .table-only:only-child) { display: none; }" in md and "td.cell-wide { max-width: none; }" in md
    templates = ROOT / "bananachat" / "templates"
    for name, marker in (("admin/access.html", '<table class="table table-stack table-stack-md access-table">'),
                         ("admin/limit_grants.html", '<table class="table table-stack table-stack-md">'),
                         ("admin/audit.html", '<table class="table table-stack table-stack-md audit-table">'),
                         ("admin/agents.html", '<table class="table table-stack table-stack-md">\n        <thead><tr><th scope="col">Session</th>'),
                         ("developer/usage.html", '<table class="table table-stack table-stack-md dev-usage">')):
        assert marker in (templates / name).read_text(), name
    assert (templates / "admin/access.html").read_text().count("table-stack-md access-table") == 3
    # Card-only rules follow the card breakpoint.
    assert "@media (max-width: 900px) {\n  .access-table .badge" in admin_css.replace(
        "/* The permission tables are cards up to 900px (.table-stack-md). */\n", "")
    developer = _css("developer.css")
    assert ".dev-usage td.dev-usage-source { flex-basis: 100%; }" in developer.split("@media (max-width: 900px)")[1]
    assert "minmax(min(100%, 440px), 1fr)" in _rule(admin_css, ".metrics-pair")


# ----- final pass ------------------------------------------------------------------------------------

TEMPLATES = ROOT / "bananachat" / "templates"


# American spellings whose British form differs. "Enrolled" and "enrolling" are British too, and a computer
# program is a program in both; a scheme such as the music programme is not.
AMERICAN = re.compile(
    r"\b(?:customiz\w*|enroll|enrolls|enrollments?|catalogs?|toward|colors?|colored|behaviors?|labeled|labeling"
    r"|centers?|centered|canceled|canceling|favorites?|organiz\w*|recogniz\w*|summariz\w*|authoriz\w*"
    r"|synchroniz\w*|normaliz\w*|randomiz\w*|initializ\w*|optimiz\w*|minimiz\w*|maximiz\w*|prioritiz\w*"
    r"|analyz\w*|utiliz\w*|sanitiz\w*|gray|music[- ]programs?|programs? (?:details|enabled|settings)"
    r"|(?:join|leave|leaving|left|joined|in|about|hide|remove) the program)\b",
    re.IGNORECASE)
# The noun is "licence"; the verb ("licensed") and the AGPL's own name keep the s.
LICENSE_NOUN = re.compile(r"(?<!Public )\bLicense\b|\blicenses?\b")


def _american(text: str) -> list[str]:
    text = re.sub(r"(?:Proxy-)?Authorization: Bearer", " ", text)  # the HTTP header's name
    return AMERICAN.findall(text) + LICENSE_NOUN.findall(text)


def _prose(markdown: str) -> str:
    """Markdown without code blocks, code spans and link targets (identifiers, settings, paths)."""
    markdown = re.sub(r"```.*?```", " ", markdown, flags=re.S)
    markdown = re.sub(r"`[^`\n]*`", " ", markdown)
    return re.sub(r"\]\([^)]*\)|https?://\S+", " ", markdown)


def _visible(html: str) -> str:
    """The text a person reads or hears on a page: content, labels, tooltips and confirmation questions."""
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S)
    said = re.findall(r'\s(?:alt|aria-label|title|placeholder|data-confirm)="([^"]*)"', html)
    return " ".join([re.sub(r"<[^>]+>", " ", html), *said])


def test_the_english_interface_and_documentation_use_british_spelling(app, admin):
    """Customise, enrolment, catalogue, colour, programme (the music scheme), towards, licence: in the English
    catalogue, on every page an administrator can open, and in the documentation and the 1.7.0 notes.
    Identifiers, settings and addresses (/customize, enrollment, catalog.py) are not prose and keep their names."""
    import json

    from bananachat.services import ollama
    from tests.app.test_admin import _admin_urls

    for path in sorted((ROOT / "bananachat" / "i18n").glob("*.json")):
        texts = json.loads(path.read_text())["en"].values()  # the keys are identifiers
        english = " ".join(" ".join(text.values()) if isinstance(text, dict) else text for text in texts)
        assert _american(english) == [], path.name
    with app.test_request_context():
        ollama.sync_catalog()
    pages = [url for _endpoint, url in _admin_urls(app, "GET") if "<" not in url and "/x" not in url
             and not url.endswith("/1")]
    pages += ["/", "/customize", "/account", "/developer", "/personalities", "/agents", "/community", "/music"]
    assert "/admin/music" in pages and "/admin/models/providers" in pages
    found = {}
    for url in pages:
        response = admin.get(url, headers={"Accept-Language": "en"})
        if response.status_code == 200 and response.mimetype == "text/html":
            found[url] = _american(_visible(response.get_data(as_text=True)))
    assert {url: words for url, words in found.items() if words} == {}
    assert "<h1>Customise</h1>" in admin.get("/customize", headers={"Accept-Language": "en"}).get_data(as_text=True)
    documents = [*sorted((ROOT / "docs").glob("*.md")),
                 *(ROOT / name for name in ("README.md", "UPGRADING.md", "CONTRIBUTING.md", "SECURITY.md"))]
    texts = {path.name: path.read_text() for path in documents}
    texts["CHANGELOG.md (1.7.0)"] = (ROOT / "CHANGELOG.md").read_text().split("\n## 1.6.0")[0]
    assert {name: _american(_prose(text)) for name, text in texts.items() if _american(_prose(text))} == {}


def test_disclosures_on_personality_and_administration_pages_use_the_chevron():
    """Like the API pages: no native triangle; a chevron that turns when the section is open."""
    templates = [*TEMPLATES.glob("personalities/*.html"), TEMPLATES / "admin" / "personality_edit.html",
                 *(path for path in TEMPLATES.glob("admin/*.html") if "inline-details" in path.read_text())]
    assert TEMPLATES / "admin" / "personalities.html" in templates
    for path in templates:
        for attributes, body in re.findall(r"<summary([^>]*)>(.*?)</summary>", path.read_text(), re.S):
            if not re.search(r'class="[^"]*\b(btn|icon-btn)\b', attributes):  # menus and buttons have their own look
                assert 'icon("chevron-down")' in body, (path.name, body)
    admin = _css("admin.css")
    assert "list-style: none" in _rule(admin, ".inline-details > summary")
    assert "display: none" in _rule(admin, ".inline-details > summary::-webkit-details-marker")
    assert "rotate(180deg)" in _rule(admin, ".inline-details[open] > summary svg")
