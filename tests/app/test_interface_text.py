"""Interface text: wording, number and date formatting, spelling, contrast and the accessible markup of forms."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.app.conftest import Browser
from tests.app.test_limits_tokens import _ensure_column, _models, _signed_in

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "bananachat" / "templates"
CSS = ROOT / "bananachat" / "static" / "css"


def _css(name: str) -> str:
    return (CSS / name).read_text()


def _rule(css: str, selector: str) -> str:
    """The declarations of the first rule whose selector list is exactly *selector*."""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, selector
    return match.group(1)


# ----- sign-in and sign-up forms --------------------------------------------------------------

def test_a_failed_sign_in_explains_itself_next_to_the_form(client):
    response = client.post("/login", {"username": "admin", "password": "wrong-password"})
    html = response.get_data(as_text=True)
    assert response.status_code == 401
    # Inline, announced, and read with the field the person has to fill in again.
    assert re.search(r'<div class="auth-messages[^"]*" id="auth-messages">\s*<div class="alert alert-error" role="alert">', html)
    assert re.search(r'<input type="password" id="password"[^>]*autofocus aria-describedby="auth-messages"', html)
    assert not re.search(r'id="username"[^>]*autofocus', html)
    assert "data-flash" not in html  # not also as a toast in the corner


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


def test_auth_pages_show_the_current_language_and_the_common_footer(client):
    html = client.get("/login", headers={"Accept-Language": "en"}).get_data(as_text=True)
    assert 'aria-current="true">English</a>' in html
    assert 'class="site-footer"' in html and "AGPL-3.0" in html


def test_the_public_language_switch_names_each_language(app):
    html = Browser(app).get("/no-such-page", headers={"Accept-Language": "en"}).get_data(as_text=True)
    assert '<span aria-hidden="true">IT</span><span class="visually-hidden">Italiano</span>' in html
    assert '<span aria-hidden="true">DE</span><span class="visually-hidden">Deutsch</span>' in html


def test_the_setup_hint_shows_the_command_name_as_code(app):
    from markupsafe import Markup

    from bananachat.i18n import translate_html

    code = Markup('<code translate="no">banana</code>')
    for language in ("en", "it", "de"):
        assert str(code) in translate_html(language, "auth.setup_token_hint_command", command=code)


# ----- formatting ---------------------------------------------------------------------------------

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


def test_the_administration_is_marked_english_for_italian_administrators(app):
    html = _signed_in(app, "admin", language="it").get("/admin/").get_data(as_text=True)
    assert '<div class="admin-shell" lang="en">' in html


def test_json_in_the_agent_log_shows_real_characters(app):
    with app.test_request_context():
        html = app.jinja_env.from_string("<pre>{{ value | pretty_json }}</pre>").render(
            value={"command": "mkdir -p src && echo 'hi'"})
    assert "&amp;&amp;" in html and "&#39;hi&#39;" in html and "\\u0026" not in html


def test_dates_in_page_text_do_not_break_inside(app):
    with app.test_request_context():
        html = str(app.jinja_env.from_string("{{ value | datetime_html }}").render(value="2026-10-09 15:14:00"))
    assert html == '<span class="nowrap">2026-10-09</span> <span class="nowrap">15:14 UTC</span>'


def test_user_pages_mark_dates_for_the_reader_and_keep_utc_as_the_fallback(app):
    template = app.jinja_env.from_string("{{ value | local_time }}|{{ value | local_time('date') }}")
    with app.test_request_context("/account"):
        html = str(template.render(value="2026-10-09 15:14:00"))
    assert html == ('<time datetime="2026-10-09T15:14:00Z" data-local="datetime"><span class="nowrap">2026-10-09</span> '
                    '<span class="nowrap">15:14 UTC</span></time>|<time datetime="2026-10-09T15:14:00Z" '
                    'data-local="date"><span class="nowrap">2026-10-09</span></time>')
    with app.test_request_context("/admin/users"):
        assert "<time" not in str(template.render(value="2026-10-09 15:14:00"))


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


def test_phrases_with_a_number_and_a_duration_do_not_break_inside():
    import json

    catalog = json.loads((ROOT / "bananachat" / "i18n" / "account.json").read_text())
    assert catalog["en"]["account.rate_burst"].endswith("(bursts\xa0of\xa0up\xa0to\xa0{burst})")
    assert catalog["it"]["account.rate_burst"].endswith("(fino\xa0a\xa0{burst}\xa0di\xa0fila)")
    assert "(in\xa0{left})" in catalog["en"]["account.weekly_resets"]
    assert "tra\xa0{left}" in catalog["it"]["account.window_resets"]


def test_action_codes_break_only_between_their_parts(app):
    from bananachat.formatting import break_points

    assert str(break_points("account.two_step_enable")) == "account.<wbr>two_<wbr>step_<wbr>enable"
    assert "&lt;b&gt;" in str(break_points("<b>"))


# ----- limits and requests ----------------------------------------------------------------------

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


def test_the_amount_hint_is_read_with_the_token_fields(app, make_user):
    make_user("una")
    page = _signed_in(app, "una").get("/account").get_data(as_text=True)
    assert re.search(r'id="quota_tokens"[^>]*aria-describedby="quota_tokens_hint"', page)
    assert 'id="quota_tokens_hint"' in page and 'id="quota_extra_hint"' in page


def test_extra_tokens_name_their_period_once(app):
    from flask import g

    from bananachat.web.account import describe_grant

    grant = {"kind": "extra", "amount": 50000, "scope": "window", "model_id": None, "pool": "chat"}
    with app.test_request_context():
        g.lang = "en"
        assert describe_grant(grant) == "+50k tokens per 5\xa0hours · Chat"
        g.lang = "it"
        assert describe_grant(grant).startswith("+50k token ogni 5\xa0ore · ")


def test_grants_in_the_administration_name_their_amount_and_period(app, admin, make_user):
    from tests.app.test_limits import _grant

    user = make_user("gwen")
    with app.app_context():
        _grant(user_id=user["id"], pool="chat", scope="window", kind="extra", amount=50_000)
    page = admin.get("/admin/quotas/grants").get_data(as_text=True)
    assert "<strong>+50k tokens per 5 hours</strong> · Chat" in page and "5-hour tokens ·" not in page


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


# ----- contrast -------------------------------------------------------------------------------------

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


# ----- quotation marks and spelling -----------------------------------------------------------------

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
    catalogue, on every page an administrator can open, in the documentation and in the current changelog entries.
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
    texts["CHANGELOG.md (since 1.7)"] = (ROOT / "CHANGELOG.md").read_text().split("\n## 1.6.0")[0]
    assert {name: _american(_prose(text)) for name, text in texts.items() if _american(_prose(text))} == {}


def test_disclosures_on_personality_and_administration_pages_use_the_chevron():
    """Like the API pages: a chevron instead of the native triangle."""
    templates = [*TEMPLATES.glob("personalities/*.html"), TEMPLATES / "admin" / "personality_edit.html",
                 *(path for path in TEMPLATES.glob("admin/*.html") if "inline-details" in path.read_text())]
    assert TEMPLATES / "admin" / "personalities.html" in templates
    for path in templates:
        for attributes, body in re.findall(r"<summary([^>]*)>(.*?)</summary>", path.read_text(), re.S):
            if not re.search(r'class="[^"]*\b(btn|icon-btn)\b', attributes):  # menus and buttons have their own look
                assert 'icon("chevron-down")' in body, (path.name, body)
