"""Full re-audit pass 8 (NC1): leftovers of earlier fixes in the core, the admin pages, two-step recovery codes and
the access log."""
from __future__ import annotations

import runpy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.app.fixtures import Browser
from tests.app.test_two_step import _enable, _sign_in, clock  # noqa: F401


# ----- NC1-1: a grant for a model id beyond 64 bits ---------------------------------------------------------

def test_nc1_1_grant_for_a_model_id_beyond_64_bits_is_a_form_error(admin, make_user, warnings_log):
    make_user("bob")
    for pool in ("model:9223372036854775808", "model:123456789012345678901234", "model:abc", "model:١"):
        response = admin.post("/admin/quotas/grants", {"target": "user", "username": "bob", "pool": pool,
                                                       "scope": "", "kind": "extra", "amount": "1000",
                                                       "duration": "forever", "reason": "x"})
        assert response.status_code == 400, pool
        assert "That model does not exist." in response.get_data(as_text=True)
    assert not [record for record in warnings_log.records if "OverflowError" in record.getMessage()]


# ----- NC1-2: the metrics CSV export -------------------------------------------------------------------------

def _audits(app, action):
    from bananachat import db
    with app.app_context():
        return db.scalar("SELECT COUNT(*) FROM audit_log WHERE action=?", (action,), 0)


def test_nc1_2_metrics_export_is_a_post_and_a_refused_one_is_not_audited(make_app):
    from bananachat import db, security

    app = make_app(download_threads=8)  # 2 places per client
    admin = Browser(app)
    admin.login("admin", "admin-password")
    with app.app_context():
        places = security._downloads()
        client = "user:" + db.scalar("SELECT id FROM users WHERE username='admin'")
    held = [places.take(client)[0] for _ in range(places.per_client)]  # two other large responses open
    assert all(held)
    assert admin.post("/admin/metrics/export.csv?range=30d").status_code == 429
    for release in held:
        release()
    assert _audits(app, "admin.metrics_export") == 0

    # A link (a top-level navigation from another site) only leads to the metrics page.
    link = admin.get("/admin/metrics/export.csv?range=30d",
                     headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})
    assert link.status_code == 302 and link.headers["Location"].endswith("/admin/metrics?range=30d")
    assert _audits(app, "admin.metrics_export") == 0
    assert 'action="/admin/metrics/export.csv?range=30d"' in admin.get("/admin/metrics?range=30d").get_data(
        as_text=True)

    response = admin.post("/admin/metrics/export.csv?range=30d")
    assert response.status_code == 200 and response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    assert _audits(app, "admin.metrics_export") == 1
    assert places.active == 0


# ----- NC1-3: recovery-code sign-ins and the audit log -------------------------------------------------------

def test_nc1_3_recovery_code_sign_ins_always_work_and_stop_adding_audit_entries(app, make_user, clock,  # noqa: F811
                                                                                  monkeypatch):
    from bananachat.web import auth

    monkeypatch.setattr(auth, "RECOVERY_CODE_AUDITS", 3, raising=False)

    def recovery_codes(username):
        make_user(username)
        browser = Browser(app)
        browser.login(username)
        return _enable(browser, username, clock)[1]

    codes = recovery_codes("mallory")
    for code in codes[:5]:
        browser, response = _sign_in(app, "mallory", code)
        # The sign-in itself is never refused for this.
        assert response.status_code == 302 and "/login" not in response.headers["Location"]
        assert browser.get("/account").status_code == 200
    assert _audits(app, "auth.two_step_recovery_code_used") == 3
    # The budget is the account's own: another account's recovery-code sign-in is still recorded.
    _browser, response = _sign_in(app, "bob", recovery_codes("bob")[0])
    assert response.status_code == 302
    assert _audits(app, "auth.two_step_recovery_code_used") == 4


# ----- NC1-4: personality share links in the access log ------------------------------------------------------

def test_nc1_4_access_log_hides_personality_share_tokens():
    pytest.importorskip("gunicorn")

    from gunicorn.config import Config

    settings = runpy.run_path(str(Path(__file__).resolve().parents[2] / "gunicorn.conf.py"))
    logger = settings["logger_class"](Config())
    response = SimpleNamespace(status="200 OK", headers=[], sent=10)

    def logged(path):
        environ = {"REQUEST_METHOD": "GET", "RAW_URI": path, "SERVER_PROTOCOL": "HTTP/1.1", "PATH_INFO": path}
        return logger.atoms(response, [], environ, timedelta(milliseconds=5))["U"]

    assert logged("/personalities/shared/Abc-secret_token123456") == "/personalities/shared/[token]"
    assert logged("/personalities/shared/Abc-secret_token123456/save") == "/personalities/shared/[token]/save"
    assert logged("//personalities//shared/Abc-secret_token123456") == "//personalities//shared/[token]"
    assert logged("//share/Abc-secret_token123456") == "//share/[token]"
    assert logged("/share/Abc-secret_token123456") == "/share/[token]"
    assert logged("/personalities/12") == "/personalities/12"
