"""Fourth review of the token limit system: fallbacks and reasoning effort, request buckets shared by processes,
window edges, effort unlocks, agents and the administration pages."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.app.test_limits_tokens import _api_key, _complete, _ensure_column, _model_policy, _models, _policy


def _reasoning(model_id, levels='["low", "medium", "high"]'):
    from bananachat import db

    _ensure_column("ai_models", "reasoning_levels", "TEXT")
    db.execute("UPDATE ai_models SET is_reasoning=1, reasoning_levels=? WHERE id=?", (levels, model_id))
    return db.one("SELECT * FROM ai_models WHERE id=?", (model_id,))


# ----- fallbacks and reasoning effort ----------------------------------------------------------------

def test_a_fallback_never_thinks_above_the_level_the_account_may_use(app, make_user, fake_ollama):
    """A request to a model that does not reason carries no ``think``; a reasoning fallback then thinks at its
    own default (medium) - above the Low a heavy model allows unless unlocked."""
    from bananachat.db import catalog

    models = _models(app)
    user = make_user("fifi")
    raw = _api_key(app, user["id"])
    with app.app_context():
        catalog.update(models["llama3.2:3b"]["id"], sort_order=0)
        catalog.update(models["qwen3:4b"]["id"], sort_order=1)
        heavy = _reasoning(models["qwen3:4b"]["id"])
        _model_policy(heavy, effort_default="low")
    fake_ollama.fail_models = {"llama3.2:3b"}
    _complete(app, raw, model="auto")
    heavy_bodies = [body for path, body in fake_ollama.requests
                    if path == "/api/chat" and body.get("model") == "qwen3:4b"]
    assert all(body.get("think") in ("low", False) for body in heavy_bodies), heavy_bodies


# ----- request buckets --------------------------------------------------------------------------------

def test_a_process_that_read_the_clock_earlier_never_moves_a_bucket_back_in_time(app):
    """Two Gunicorn processes read the clock before waiting for the write lock: the later writer must not store
    an older time, or the next request refills the same interval twice."""
    from bananachat.db import limits as store

    with app.app_context():
        bucket = [("api:someone:1/second/1", 1.0, 1)]
        assert store.take_tokens(bucket, 100.0)[0]           # the bucket is empty now
        assert store.take_tokens(bucket, 101.0)[0]           # process B: refilled after one second
        assert not store.take_tokens(bucket, 100.5)[0]       # process A, clock read before B committed
        # Half a second after B's request only half a request has come back.
        assert not store.take_tokens(bucket, 101.5)[0]


# ----- window edges -----------------------------------------------------------------------------------

def test_the_request_that_opens_a_window_counts_in_it_across_a_second_boundary(app, make_user, monkeypatch):
    """The ledger row and the window start were stamped by two clock reads: when a second boundary fell between
    them, the first request of every window was never counted."""
    from bananachat import db
    from bananachat.db import credits
    from bananachat.services import limits

    models = _models(app)
    user = make_user("edge")
    with app.app_context():
        _policy("api", window={"tokens": 10_000, "slow_tokens": 0})
        real_now = db.now
        monkeypatch.setattr(db, "now", lambda offset=None: real_now((offset or timedelta()) - timedelta(seconds=1)))
        credits.charge(user["id"], 4000, 0, request_type="api", model_id=models["llama3.2:3b"]["id"])
        monkeypatch.setattr(db, "now", real_now)
        assert limits.effective(user, "api").window.used == 4000


# ----- reasoning-effort unlocks -----------------------------------------------------------------------

def test_an_unlock_for_every_model_also_lifts_models_unlocked_one_at_a_time(app, admin, make_user):
    """An automatic unlock stores a level for one model; approving "Max on every model" afterwards must not leave
    that model capped at the lower automatic level (only an administrator's level for a model is a lock)."""
    from bananachat.db import credits
    from bananachat.db import limits as store
    from bananachat.services import limits

    models = _models(app)
    user = make_user("uma")
    with app.app_context():
        model = _reasoning(models["qwen3:4b"]["id"], '["low", "medium", "high", "max"]')
        store.set_effort_level(user["id"], model["id"], "high", source="automatic")
        outcome = credits.submit_request(user["id"], kind="effort", level="max", reason="Hard proofs, please")
        credits.resolve_request(outcome["id"], None, True)
        assert limits.effort_ceiling(user, model) == "max"
        # An administrator's level for one model still locks it below the level for every model.
        store.set_effort_level(user["id"], model["id"], "low", source="admin")
        assert limits.effort_ceiling(user, model) == "low"
        with pytest.raises(limits.EffortLocked):
            limits.resolve_effort(user, model, "medium")


# ----- messages and pages -----------------------------------------------------------------------------

def test_a_models_rate_refusal_names_the_rate_in_the_readers_language(app, make_user):
    """The chat shows the refusal in the account's language; the rate inside it was always English."""
    from bananachat.services import limits

    models = _models(app)
    user = make_user("rita")
    with app.app_context():
        _model_policy(models["qwen3:4b"], enabled=True, rate_rules=[{"requests": 1, "per": "minute"}])
        assert limits.admit(user, "chat", models["qwen3:4b"]).allowed
        refusal = limits.admit(user, "chat", models["qwen3:4b"]).refusal
    assert "1 richiesta al minuto" in refusal.message("it") and "per minute" not in refusal.message("it")
    assert "1 request per minute" in refusal.message("en")


def test_the_developer_pages_speak_of_limits_not_credits(app, make_user):
    """Credits are gone: the token list and the playground still told people they spend credits."""
    from bananachat.i18n import translate

    for key in ("developer.tokens_text", "developer.playground_intro"):
        assert "credit" not in translate("en", key).lower() and "credit" not in translate("it", key).lower()


def test_the_grant_form_keeps_what_was_typed_and_never_defaults_to_everyone(app, admin, make_user):
    """A mistyped amount sent the administrator back to an empty form; and the form started on "Everyone",
    "Unlimited", "Every service", so one click on Create grant lifted every limit of every account for a day."""
    from bananachat.db import limits as store

    make_user("gina")
    page = admin.get("/admin/quotas/grants").get_data(as_text=True)
    assert 'name="target" value="everyone" checked' not in page and 'name="target" value="user" checked' not in page
    assert admin.post("/admin/quotas/grants", {"kind": "unlimited", "duration": "24h"}).status_code != 302
    with app.app_context():
        assert store.list_grants("active") == []
    typed = {"target": "user", "username": "gina", "kind": "extra", "amount": "lots", "pool": "api",
             "scope": "window", "duration": "7d", "reason": "Exam week for the whole class"}
    response = admin.post("/admin/quotas/grants", typed)
    page = response.get_data(as_text=True)
    assert response.status_code == 400 and "Extra tokens: write a number of tokens" in page
    assert 'value="gina"' in page and ">Exam week for the whole class</textarea>" in page and 'value="lots"' in page
    assert 'value="extra" selected' in page and 'value="api" selected' in page and 'value="window" selected' in page
    assert 'name="target" value="user" checked' in page and 'name="duration" value="7d" checked' in page


def test_changing_one_models_limits_for_an_account_keeps_its_other_settings(app, admin, make_user):
    """The account page had one empty form for every model: setting a weekly limit for a locked model unlocked
    it, and locking a model wiped its custom limits. A model with custom limits now has its own filled-in form."""
    import re

    from bananachat.db import limits as store

    models = _models(app)
    big = models["qwen3:4b"]
    user = make_user("lou")
    with app.app_context():
        store.set_model_override(user["id"], big["id"], None, locked=True, window_tokens=40_000)
    page = admin.get(f"/admin/users/{user['id']}/limits").get_data(as_text=True)
    own = page.split(f'id="model-form-{big["id"]}"', 1)[1].split("</form>", 1)[0]
    assert 'name="locked" value="1" checked' in own and 'value="40k"' in own
    # The form for other models no longer offers one that has its own form.
    generic = page.split('id="model-override-model"', 1)[1].split("</select>", 1)[0]
    assert f'value="{big["id"]}"' not in generic and f'value="{models["llama3.2:3b"]["id"]}"' in generic
    # Sending the filled-in form back with a weekly limit added keeps the lock and the 5-hour limit.
    fields = {}
    for tag in re.findall(r"<input[^>]*>", own):
        name, value = re.search(r'name="(\w+)"', tag), re.search(r'value="([^"]*)"', tag)
        if name and value and (' type="checkbox"' not in tag or " checked" in tag):
            fields[name.group(1)] = value.group(1)
    fields.update(weekly_tokens="100k", locked="1")
    fields.pop("csrf_token", None)
    assert admin.post(f"/admin/users/{user['id']}/limits/model", fields).status_code == 302
    with app.app_context():
        override = store.get_model_override(user["id"], big["id"])
        assert (override.locked, override.window_tokens, override.weekly_tokens) == (True, 40_000, 100_000)


# ----- round 2: grants, auto, tiers ------------------------------------------------------------------

def test_an_unlimited_grant_for_every_service_also_lifts_model_limits(app, make_user):
    """Administrators granting "Unlimited, every service" expect no limit at all; a grant for one service or a
    multiplier leaves a model's own limits (counted across services) alone."""
    from bananachat import db
    from bananachat.db import credits
    from bananachat.db import limits as store
    from bananachat.services import limits

    models = _models(app)
    big = models["qwen3:4b"]
    user = make_user("ulla")
    with app.app_context():
        _model_policy(big, enabled=True, window_tokens=1000, rate_rules=[{"requests": 1, "per": "hour"}])
        credits.charge(user["id"], 1000, 0, request_type="api", model_id=big["id"])
        assert limits.admit(user, "api", big).refusal.key == "model_window"
        for pool, kind in (("api", "unlimited"), (None, "multiplier")):
            store.create_grant(created_by=None, user_id=user["id"], pool=pool, scope=None, kind=kind,
                               amount=2 if kind == "multiplier" else 0, starts_at=db.now(), ends_at=None, reason="")
        assert limits.model_limits(user, big).window.tokens == 1000
        assert not limits.admit(user, "api", big).allowed
        store.create_grant(created_by=None, user_id=user["id"], pool=None, scope=None, kind="unlimited", amount=0,
                           starts_at=db.now(), ends_at=None, reason="Conference demo")
        current = limits.model_limits(user, big)
        assert not current.window.limited and not current.rate.limited
        assert limits.admit(user, "api", big).allowed and limits.admit(user, "chat", big).allowed


def test_an_automatically_approved_amount_never_holds_an_account_below_its_tier(app, admin, make_user):
    """An approved request becomes a custom limit, and custom limits replace the tier: a small raise approved
    automatically froze the account below what later promotions give. Automatic amounts are now a floor; an
    amount an administrator typed stays exact."""
    from bananachat.db import credits
    from bananachat.db import limits as store
    from bananachat.db import settings as site_settings
    from bananachat.services import limits

    user = make_user("tia")
    with app.app_context():
        _policy("api", window={"tokens": 30_000, "slow_tokens": 0, "auto_tiers": True})
        site_settings.update(quota_auto_approve_enabled=1, quota_auto_approve_max_tokens=50_000)
        assert credits.submit_request(user["id"], 40_000, None, "A bigger project", kind="window",
                                      pool="api")["status"] == "approved"
        assert limits.effective(user, "api").window.tokens == 40_000
        regular = store.list_tiers()[1]  # ×2
        store.update_user_settings(user["id"], None, tier_id=regular["id"])
        assert limits.effective(user, "api").window.tokens == 60_000
        assert limits.base_limits(user["id"], "api")["window_tokens"] == 60_000
    page = admin.get(f"/admin/users/{user['id']}/limits").get_data(as_text=True)
    assert "Approved automatically, so never below the tier's amount: slow tokens per 5 hours, tokens per 5 hours." \
        in page
    # Saving the account's form unchanged keeps the amount automatic ...
    form = {"pool": "api", "window_tokens": "40k", "window_slow_tokens": "", "weekly_tokens": ""}
    assert admin.post(f"/admin/users/{user['id']}/limits/custom", form).status_code == 302
    with app.app_context():
        assert limits.effective(user, "api").window.tokens == 60_000
    # ... an amount the administrator types is exact, even below the tier.
    assert admin.post(f"/admin/users/{user['id']}/limits/custom", {**form, "window_tokens": "20k"}).status_code == 302
    with app.app_context():
        assert limits.effective(user, "api").window.tokens == 20_000


def test_auto_moves_to_a_model_outside_a_used_up_service(app, make_user, fake_ollama):
    """With the service's tokens used up, ``auto`` refused although a model that does not count toward them
    could answer (chat refused before even choosing a model)."""
    from bananachat.db import catalog, credits
    from tests.app.test_chat import new_chat, send, wait_idle
    from tests.app.test_limits_tokens import _signed_in

    models = _models(app)
    user = make_user("otis")
    raw = _api_key(app, user["id"])
    with app.app_context():
        catalog.update(models["llama3.2:3b"]["id"], sort_order=0)
        catalog.update(models["qwen3:4b"]["id"], sort_order=1)
        _model_policy(models["qwen3:4b"], counts_toward_pool=False)
        _policy("api", window={"tokens": 1000, "slow_tokens": 0})
        _policy("chat", window={"enabled": True, "tokens": 1000, "slow_tokens": 0})
        for pool in ("api", "chat"):
            credits.charge(user["id"], 1000, 0, request_type=pool, model_id=models["llama3.2:3b"]["id"])
    answered = _complete(app, raw, model="auto")
    assert answered.status_code == 200 and answered.json["model"] == "qwen3:4b"
    assert _complete(app, raw, model="llama3.2:3b").status_code == 429
    browser = _signed_in(app, "otis")
    session_id = new_chat(browser)
    response = send(browser, session_id, model="auto")
    assert response.status_code == 200, response.get_data(as_text=True)
    wait_idle(app, session_id)
    assert [body for path, body in fake_ollama.requests if path == "/api/chat"][-1]["model"] == "qwen3:4b"
    assert send(browser, session_id, model="llama3.2:3b").status_code == 429


def test_refused_limit_forms_come_back_as_typed(app, admin, make_user):
    """Every Limits form redirected to the saved values after an error, so a typo lost the whole form."""
    import re

    models = _models(app)
    user = make_user("fern")
    big, small = models["qwen3:4b"], models["llama3.2:3b"]

    def refused(url, fields, *expected):
        response = admin.post(url, fields)
        page = response.get_data(as_text=True)
        assert response.status_code == 400, (url, response.status_code)
        for text in expected:
            assert re.search(text, page), (url, text)
        return page

    refused("/admin/quotas/policy/api",
            {"rate_enabled": "1", "rule_requests_0": "7", "rule_per_0": "hour", "rule_burst_0": "3",
             "window_enabled": "1", "window_tokens": "50kk", "window_slow_tokens": "5k", "weekly_tokens": "900k",
             "weekly_enabled": "1"},
            'value="50kk"', 'value="900k"', r'name="rule_requests_0"[^>]*value="7"', r'<option value="hour" selected>',
            r'name="weekly_enabled" value="1" checked')
    refused("/admin/quotas", {"quota_auto_approve_enabled": "1", "quota_auto_approve_max_tokens": "lots"},
            'value="lots"', r'name="quota_auto_approve_enabled" value="1" checked')
    page = refused(f"/admin/quotas/models/{big['id']}",
                   {"action": "save", "weight": "3", "sensitivity": "2", "enabled": "1", "window_tokens": "abc",
                    "weekly_tokens": "1M", "counts_toward_pool": "no", "rule_requests_0": "6", "rule_per_0": "minute"},
                   'value="abc"', 'value="1M"', r'<option value="no" selected>')
    assert f'id="model-{big["id"]}" open' in page
    refused(f"/admin/users/{user['id']}/limits/custom", {"pool": "chat", "window_tokens": "12q", "weekly_tokens": "300k"},
            'value="12q"', 'value="300k"', r'id="pool-chat"')
    refused(f"/admin/users/{user['id']}/limits/model",
            {"model_id": str(small["id"]), "window_tokens": "zz", "locked": "1"},
            'value="zz"', rf'<option value="{small["id"]}" selected>', r'name="locked" value="1" checked')
    refused("/admin/quotas/effort",
            {"effort_gating_enabled": "1", "effort_default_level": "low", "effort_auto_active_days": "40",
             "effort_auto_tokens": "3M", "effort_auto_period_days": "30", "effort_auto_clean_days": "90",
             "effort_auto_ceiling": "max"},
            'value="3M"', 'value="40"', r'<option value="low" selected>', r'<option value="max" selected>')


# ----- round 2: queries on the hot path ---------------------------------------------------------------

LIMIT_TABLES = ("limit_policy", "model_limit_policy", "user_limits", "user_limit_overrides", "user_model_limits",
                "limit_grants", "limit_windows", "credit_ledger", "image_credit_reservations", "rate_buckets",
                "user_effort_levels", "limit_tiers", "runtime_state")


def _limit_selects(app, action):
    """Run *action* and return the SELECT statements on limit tables, in every thread's connection."""
    import re

    from bananachat import db

    statements = []
    original = db.conn
    traced = set()

    def traced_conn():
        connection = original()
        if id(connection) not in traced:
            traced.add(id(connection))
            connection.set_trace_callback(statements.append)
        return connection

    db.conn = traced_conn
    try:
        result = action()
    finally:
        db.conn = original
        with app.app_context():
            original().set_trace_callback(None)
    pattern = re.compile(r"\b(FROM|JOIN)\s+(" + "|".join(LIMIT_TABLES) + r")\b")
    return result, [text for text in statements if text.lstrip().upper().startswith("SELECT") and pattern.search(text)]


def test_one_api_completion_reads_each_limit_setting_once(app, make_user, fake_ollama):
    """One API completion with ``auto`` and a fallback read the limit tables about 70 times (every check loaded
    policies, grants, windows and settings again)."""
    from bananachat.db import catalog

    models = _models(app)
    user = make_user("perf")
    raw = _api_key(app, user["id"])
    with app.app_context():
        catalog.update(models["llama3.2:3b"]["id"], sort_order=0)
        catalog.update(models["qwen3:4b"]["id"], sort_order=1)
        _policy("api", window={"tokens": 50_000, "dynamic": True}, weekly={"enabled": True, "tokens": 200_000})
        _model_policy(models["qwen3:4b"], enabled=True, window_tokens=10_000,
                      rate_rules=[{"requests": 5, "per": "minute"}])
    assert _complete(app, raw, model="auto").status_code == 200  # warm the per-process caches
    response, selects = _limit_selects(app, lambda: _complete(app, raw, model="auto"))
    assert response.status_code == 200 and response.json["model"] == "llama3.2:3b"
    assert len(selects) <= 40, "\n".join(selects)


def test_a_charge_never_runs_the_30_day_usage_query_under_the_write_lock(app, make_user):
    """Charging decides the lane with the account's dynamic factor; on a cache miss that meant a 30-day ledger
    query while holding SQLite's write lock (every process waits). Admission computes it; the charge reuses it."""
    from bananachat.db import credits
    from bananachat.services import limits

    models = _models(app)
    user = make_user("lock")
    with app.app_context():
        _policy("api", window={"tokens": 50_000, "dynamic": True})
        limits.forget_personal()
        _result, selects = _limit_selects(app, lambda: credits.charge(user["id"], 100, 0, request_type="api",
                                                                      model_id=models["llama3.2:3b"]["id"]))
        assert not [text for text in selects if "GROUP BY day, hour" in text]
        assert limits.admit(user, "api", models["llama3.2:3b"]).allowed  # admission computes it ...
        _result, selects = _limit_selects(app, lambda: credits.charge(user["id"], 100, 0, request_type="api",
                                                                      model_id=models["llama3.2:3b"]["id"]))
        assert not [text for text in selects if "GROUP BY day, hour" in text]  # ... and the charge reuses it
        assert limits.effective(user, "api").window.used == 200
