"""Role x endpoint matrix: every route, every method, for each kind of caller.

BananaChat views are opened one by one (``login_required``/``admin_required``),
so the few views anybody may reach are listed here: adding a public route means
adding it below on purpose.

* anonymous visitors get a redirect to sign-in (401 for JSON) from every other view;
* a suspended account whose session row is still there reaches nothing private;
* users never get a successful answer from an administrator view;
* nobody ever gets a server error.

State-changing requests carry a valid CSRF token and an empty JSON body: a view
that acts without its parameters shows up as a success.
"""

from __future__ import annotations

import pytest

PUBLIC = {
    "auth.login", "auth.signup", "auth.setup", "auth.admin_access", "auth.logout",
    "core.favicon", "core.health", "core.robots", "core.set_language", "core.source", "core.status_report",
    "developer.legacy_index", "developer.legacy_playground", "developer.legacy_usage",
    "chat.shared", "chat.shared_download", "personalities.shared",
}
TOKEN_BLUEPRINTS = ("api_v1.", "worker_api.", "remote_api.")  # authenticate their own bearer tokens
SKIP = {"auth.logout"}  # would end the caller's own session half-way through


def _probe(app, browser, role, make_user):
    from bananachat import db, security

    victim = make_user("victim")
    if role != "anon":
        make_user("attacker", role="admin" if role == "admin" else "user")
        browser.login("attacker")
        if role == "suspended":
            with app.test_request_context():
                db.execute("UPDATE users SET suspended=1 WHERE username='attacker'")
    security.allow = lambda *args, **kwargs: True
    values = {"user_id": victim["id"], "username": "victim"}
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if rule.endpoint == "static" or rule.endpoint in SKIP:
            continue
        url = rule.build({name: values.get(name, 1) for name in rule.arguments}, append_unknown=False)[1]
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            if method == "GET":
                response = browser.get(url)
            else:
                response = browser.post_json(url, {}, method=method)
            yield rule, method, response.status_code, response.headers.get("Location", "")


@pytest.mark.parametrize("role", ["anon", "suspended", "user", "admin"])
def test_no_route_fails_and_private_routes_stay_private(app, client, make_user, monkeypatch, role):
    from bananachat import security

    monkeypatch.setattr(security, "allow", security.allow)  # restored after the probe disables limits
    problems = []
    for rule, method, status, location in _probe(app, client, role, make_user):
        where = f"{role} {method} {rule.rule} -> {status} {location}"
        if status >= 500 and status != 503:  # 503: a switched-off service (workers, cloud sessions)
            problems.append(f"server error: {where}")
        ok = 200 <= status < 300
        public = rule.endpoint in PUBLIC or rule.endpoint.startswith(TOKEN_BLUEPRINTS)
        refused = status in (400, 401, 403, 404, 405, 503) or (status == 302 and location.startswith("/login"))
        if role in ("anon", "suspended") and not public and not refused:
            problems.append(f"private view reachable: {where}")
        if role == "user" and rule.rule.startswith("/admin") and (
                ok or (status in (301, 302, 303, 307) and location.startswith("/admin"))):
            problems.append(f"administrator view reached: {where}")
    assert not problems, "\n".join(problems)
