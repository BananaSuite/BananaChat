"""Interface text filters: values put into pages and translated sentences stay escaped."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


def test_json_in_the_agent_log_shows_real_characters(app):
    with app.test_request_context():
        html = app.jinja_env.from_string("<pre>{{ value | pretty_json }}</pre>").render(
            value={"command": "mkdir -p src && echo 'hi'"})
    assert "&amp;&amp;" in html and "&#39;hi&#39;" in html and "\\u0026" not in html


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


def test_action_codes_break_only_between_their_parts(app):
    from bananachat.formatting import break_points

    assert str(break_points("account.two_step_enable")) == "account.<wbr>two_<wbr>step_<wbr>enable"
    assert "&lt;b&gt;" in str(break_points("<b>"))

