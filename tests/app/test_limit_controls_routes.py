"""The admin forms retain all four rules and save explicit exemption controls."""

import pytest

from tests.app.fixtures import Browser
from tests.app.test_chat import setup_models


def _model(app):
    from bananachat.db import catalog

    setup_models(app)
    with app.app_context():
        return catalog.get_by_name("qwen3:4b")


def _rules():
    return [{"requests": 1, "burst": 2, "per": unit} for unit in ("second", "minute", "hour", "day")]


def _rule_form():
    return {f"rule_{key}_{i}": str(value) for i, rule in enumerate(_rules()) for key, value in rule.items()}


def test_admin_model_forms_render_and_preserve_fourth_daily_rule(app, admin, make_user):
    from bananachat.db import limits as store

    model = _model(app)
    user = make_user("daily-rules")
    with app.app_context():
        store.set_model_policy(model["id"], {"enabled": True, "rate_rules": _rules()}, None)
        store.set_model_override(user["id"], model["id"], None, rate_rules=_rules())
    html = admin.get("/admin/quotas/models").get_data(as_text=True)
    assert f'id="m{model["id"]}-rules-requests-3"' in html
    html = admin.get(f'/admin/users/{user["id"]}/limits').get_data(as_text=True)
    assert f'id="model-{model["id"]}-rules-requests-3"' in html
    response = admin.post(f'/admin/quotas/models/{model["id"]}', {
        "action": "save", "enabled": "1", "weight": "1", "sensitivity": "1",
        "counts_toward_pool": "default", "effort_default": "", **_rule_form()})
    assert response.status_code == 302
    response = admin.post(f'/admin/users/{user["id"]}/limits/model', {"model_id": model["id"], **_rule_form()})
    assert response.status_code == 302
    with app.app_context():
        assert store.get_model_policy(model)["rate_rules"] == _rules()
        assert list(store.get_model_override(user["id"], model["id"]).rate_rules) == _rules()


def test_account_exemptions_are_admin_only_and_restore_clears_them(app, admin, make_user):
    from bananachat.db import limits as store

    user = make_user("route-controls")
    route = f'/admin/users/{user["id"]}/limits/exemptions'
    browser = Browser(app)
    browser.login("route-controls")
    assert browser.post(route, {"token_exempt": "1", "rate_exempt": "1"}).status_code == 403
    assert admin.post(route, {"token_exempt": "1"}).status_code == 302
    with app.app_context():
        prefs = store.user_settings(user["id"])
        assert prefs.token_exempt and not prefs.rate_exempt
    html = admin.get(f'/admin/users/{user["id"]}/limits').get_data(as_text=True)
    assert 'name="token_exempt" value="1" checked' in html
    assert admin.post(f'/admin/users/{user["id"]}/limits/restore').status_code == 302
    with app.app_context():
        assert not store.user_settings(user["id"]).token_exempt


def test_new_model_flags_save_independently_and_legacy_posts_keep_them(app, admin, make_user):
    from bananachat.db import limits as store

    model = _model(app)
    user = make_user("model-route-controls")
    route = f'/admin/users/{user["id"]}/limits/model'
    assert admin.post(route, {"model_id": model["id"], "limit_controls": "1", "token_exempt": "1",
                              "rate_exempt": "1", "locked": "1"}).status_code == 302
    assert admin.post(route, {"model_id": model["id"], "window_tokens": "4k", "locked": "1"}).status_code == 302
    with app.app_context():
        current = store.get_model_override(user["id"], model["id"])
        assert current.token_exempt and current.rate_exempt and current.locked and current.window_tokens == 4000
    assert admin.post(route, {"model_id": model["id"], "action": "clear"}).status_code == 302
    with app.app_context():
        assert store.get_model_override(user["id"], model["id"]) is None


def test_global_model_and_effort_controls_preserve_other_customizations(app, admin):
    from bananachat.db import limits as store

    model = _model(app)
    route = f'/admin/quotas/models/{model["id"]}'
    form = {"action": "save", "enabled": "1", "weight": "3", "sensitivity": "2", "dynamic": "1",
            "window_tokens": "50k", "weekly_tokens": "100k", "effort_default": "medium",
            "counts_toward_pool": "yes", "limit_controls": "1", "rate_enabled": "1",
            "effort_gating_off": "1", "effort_auto_unlock_off": "1", **_rule_form()}
    assert admin.post(route, form).status_code == 302
    with app.app_context():
        current = store.get_model_policy(model)
        assert not current["tokens_enabled"] and current["rate_enabled"]
        assert current["effort_gating_off"] and current["effort_auto_unlock_off"]
    response = admin.post(f'/admin/quotas/effort/models/{model["id"]}', {
        "effort_default": "extra", "limit_controls": "1", "effort_auto_unlock_off": "1"})
    assert response.status_code == 302
    with app.app_context():
        current = store.get_model_policy(model)
        assert not current["tokens_enabled"] and current["rate_enabled"]
        assert current["weight"] == 3 and current["window_tokens"] == 50_000 and current["dynamic"]
        assert current["effort_default"] == "extra" and not current["effort_gating_off"]
        assert current["effort_auto_unlock_off"]


@pytest.mark.parametrize("unit", ["second", "minute", "hour", "day"])
def test_all_api_keys_share_account_rate_in_every_unit_and_playground(app, make_user, monkeypatch, unit):
    from bananachat.db import limits as store, tokens
    from bananachat.services import limits

    user = make_user("shared-rate")
    other = make_user("separate-rate")
    browser = Browser(app)
    browser.login("shared-rate")
    with app.app_context():
        raw = [tokens.create(user["id"], name)[1] for name in ("first", "second")]
        other_raw = tokens.create(other["id"], "other")[1]
        policy = store.get_policy("api")
        policy["rate"].update(enabled=True, rules=[{"requests": 1, "per": unit, "burst": 1}])
        store.set_policy("api", policy, None)
    frozen = limits.time.time()
    monkeypatch.setattr(limits.time, "time", lambda: frozen)
    client = app.test_client()
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {raw[0]}"}).status_code == 200
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {raw[1]}"}).status_code == 429
    response = browser.post_json("/developer/playground/send", {"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 429
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {other_raw}"}).status_code == 200


def test_api_omitted_effort_is_refused_when_all_model_tiers_are_locked(app, make_user):
    from bananachat import db
    from bananachat.db import limits as store, tokens

    model = _model(app)
    user = make_user("locked-default-api")
    with app.app_context():
        db.execute("UPDATE ai_models SET is_reasoning=1,reasoning_levels='[\"low\",\"medium\",\"high\"]' "
                   "WHERE id=?", (model["id"],))
        store.set_effort_level(user["id"], model["id"], "off", pinned=True)
        raw = tokens.create(user["id"], "api")[1]
    response = app.test_client().post("/v1/chat/completions", json={
        "model": model["ollama_name"], "messages": [{"role": "user", "content": "Hi"}]},
        headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 403
    assert response.json["error"]["code"] == "reasoning_effort_locked"


def test_extra_effort_request_approval_cascades_after_high_without_unlocking_max(app, admin, make_user):
    from bananachat import db
    from bananachat.db import catalog, credits
    from bananachat.services import limits

    model = _model(app)
    user = make_user("request-extra")
    with app.app_context():
        db.execute("UPDATE ai_models SET is_reasoning=1,reasoning_levels=? WHERE id=?",
                   ('["low","medium","high","extra","max"]', model["id"]))
        model = catalog.get(model["id"])
    browser = Browser(app)
    browser.login("request-extra")
    for level, expected in (("high", ("low", "medium", "high")),
                            ("extra", ("low", "medium", "high", "extra"))):
        response = browser.post("/account/quota-request", {"kind": "effort", "effort_model": model["ollama_name"],
                                                           "effort_level": level, "reason": "Detailed research"})
        assert response.status_code == 302
        with app.app_context():
            row = credits.pending_request(user["id"])
            assert row["effort_level"] == level
        assert admin.post(f'/admin/quotas/requests/{row["id"]}', {"decision": "approve"}).status_code == 302
        with app.app_context():
            assert limits.allowed_efforts(user, model) == expected


def test_disabled_token_budgets_reject_pointless_quota_requests_and_extra_grants(app, admin, make_user):
    from bananachat.db import limits as store, credits

    model = _model(app)
    user = make_user("exempt-request")
    with app.app_context():
        store.update_user_settings(user["id"], None, token_exempt=True)
        store.set_model_policy(model["id"], {"enabled": True, "window_tokens": 100,
                                           "tokens_enabled": False}, None)
    browser = Browser(app)
    browser.login("exempt-request")
    assert browser.post("/account/quota-request", {"kind": "window", "pool": "api", "new_tokens": "100k",
                                                   "reason": "More usage"}).status_code == 400
    with app.app_context():
        assert credits.pending_request(user["id"]) is None
    response = admin.post("/admin/quotas/grants", {"target": "everyone", "pool": f'model:{model["id"]}',
                         "scope": "window", "kind": "extra", "amount": "5k", "duration": "24h"})
    assert response.status_code == 400
    assert "has no 5-hour limit" in response.get_data(as_text=True)
