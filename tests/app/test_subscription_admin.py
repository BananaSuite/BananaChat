"""The Claude and Codex pool pages show what each pool knows and change only their own provider's accounts.

Stale or missing telemetry is labelled as such and never grants capacity. Allowance caps are whole shares of
the provider's own allowance, family caps are kept per family, and a refused form changes nothing. Adding an
account, moving it to another profile or removing it needs a recent confirmation; checks and refreshes do not.
Only the adapter's own credential-free errors are shown; any other failure is logged by its type alone.
Fake adapters stand in for the CLIs, so nothing here signs in to a provider.
"""

import logging
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from bananachat.db import catalog
from bananachat.db import settings as site_settings
from bananachat.db import subscription_pool as accounts
from bananachat.services import subscription_pool as pool
from tests.app.fixtures import Browser
from tests.app.test_two_step import ADMIN_PASSWORD, _stale

MODEL = "provider-model-id"
FAMILIES = {"claude": "sonnet", "codex": "sol"}
BINDINGS = {"claude": "claude_code_profiles", "codex": "codex_cli_profiles"}


def descriptor(provider="claude", **changes):
    return {"name": MODEL, "family": FAMILIES[provider], "display": "Discovered model",
            "reasoning": ["low", "medium", "high", "xhigh"], "capabilities": ["completion"], **changes}


def answer(account, model, messages, options, *, cancel):
    cancel.check()
    yield {"text": "Answer"}
    yield {"tokens_in": 8, "tokens_out": 2, "done": True, "finish_reason": "stop"}


def observation(**changes):
    return {"available": True, "source": "claude_code", "observed_at": time.time(), "subscription_type": "max",
            "window_left": 1.0, "weekly_left": 1.0, "model_limits": {}, **changes}


class ConnectorError(RuntimeError):
    """Stands in for the built-in adapters' credential-free error, whose messages administrators may read."""


class NativeAuthenticationError(ConnectorError):
    """The profile is not signed in to a subscription."""


class ProfileAdapter:
    """The surface of a built-in CLI adapter (profiles, bindings, checks, reports) without a CLI behind it."""

    def __init__(self, provider):
        self.provider = provider
        self.bindings_key = BINDINGS[provider]
        self.profiles = {"primary": {}, "secondary": {}}
        self.models = [descriptor(provider)]
        self.reports = {}
        self.calls = []
        self.failure = None
        self.usage_failure = None

    def bindings(self):
        value = site_settings.state_get(self.bindings_key, {})
        return value if isinstance(value, dict) else {}

    def profile_for(self, account):
        name = self.bindings().get(str(account["id"]), account["label"])
        if name not in self.profiles:
            raise ConnectorError("Select a configured profile for this account.")
        return name, self.profiles[name]

    def check_profile(self, name, *, cancel=None):
        self.calls.append(("check", name))
        if self.failure is not None:
            raise self.failure
        return {"authenticated": True}

    def invalidate_models(self):
        self.calls.append(("invalidate",))

    def usage_reports(self, force=False, *, cancel=None):
        self.calls.append(("usage", force))
        if self.usage_failure is not None:
            raise self.usage_failure
        return dict(self.reports)

    def quota(self):
        return {"window_left": None, "weekly_left": None, "account_reports": self.usage_reports()}

    def discover(self, *, cancel=None):
        return [dict(item) for item in self.models]

    def chat(self, account, model_name, messages, options, *, cancel=None):
        yield {"text": "Answer"}
        yield {"tokens_in": 2, "tokens_out": 1, "done": True, "finish_reason": "stop"}


def _install(monkeypatch, adapter, provider):
    module = ModuleType(f"bc_test_{provider}_admin_adapter")
    module.create_adapter = lambda config: adapter
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module.__name__


def _form(**fields):
    return {"status": "active", "priority": "0", "window_limit": "100k", "note": "", **fields}


def _page(browser, provider="claude"):
    return browser.get(f"/admin/models/{provider}").get_data(as_text=True)


def _admin_log(monkeypatch, caplog):
    # The app's own handler stops propagation and the test app logs errors only.
    monkeypatch.setattr(logging.getLogger("bananachat"), "propagate", True)
    caplog.set_level(logging.WARNING, logger="bananachat.admin")


@pytest.fixture
def adapter(app):
    pool.register_site_chat("claude", answer)
    pool.register_site_discovery("claude", lambda: [descriptor()])
    yield
    pool.reset_transport()


@pytest.fixture
def adapters():
    return {provider: ProfileAdapter(provider) for provider in pool.PROVIDERS}


@pytest.fixture
def site(make_app, monkeypatch, adapters):
    """An app whose Claude and Codex settings name the built-in style adapters above."""
    app = make_app(**{f"{provider.upper()}_EXTENSION": _install(monkeypatch, adapter, provider)
                      for provider, adapter in adapters.items()})
    yield app
    pool.reset_transport()


@pytest.fixture
def browser(site):
    browser = Browser(site)
    browser.login("admin")
    return browser


# ----- subscription readout ---------------------------------------------------------------------------------

@pytest.fixture
def telemetry(app):
    pool.register_site_chat("claude", lambda *args: iter([{"text": "answer", "done": True}]))
    pool.register_site_discovery("claude", lambda: [{"name": "display-sonnet", "family": "sonnet", "reasoning": []}])
    aid = accounts.add_account("Display account", window_limit=100_000)
    report = {"source": "claude_code", "available": False, "status": "stale",
              "observed_at": time.time() - 1_200, "subscription_type": "max",
              "window_left": 0.0, "window_resets_at": time.time() + 1_800,
              "weekly_left": 0.84, "weekly_resets_at": time.time() + 100_000, "model_limits": {}}
    pool.register_site_reporter("claude", lambda: {"account_reports": {str(aid): dict(report)}})
    yield aid, report
    pool.reset_transport()


def test_admin_retains_plan_and_original_stale_percentages_without_routing(app, admin, telemetry):
    aid, report = telemetry
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert "Subscription: Claude Max" in html
    assert "Last known usage · stale." in html
    assert "Requests are paused until a fresh report is available" in html
    assert "5-hour remaining: 0.0%" in html
    assert "Weekly remaining: 84.0%" in html
    assert "Local token budget: 0 / 100,000" in html
    assert pool.account_reports("claude")[str(aid)]["observed_at"] == report["observed_at"]
    assert not pool.usable(accounts.get(aid))


def test_fresh_observation_replaces_stale_label_and_can_resume_routing(app, admin, telemetry):
    aid, report = telemetry
    admin.get("/admin/models/claude")
    report.update(status="available", available=True, observed_at=time.time(), window_left=0.9)
    pool.refresh_quota("claude")
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert "Last known usage · stale." not in html
    assert "5-hour remaining: 90.0%" in html
    assert pool.usable(accounts.get(aid))


def test_refresh_explains_staleness_without_claiming_success(admin, telemetry):
    response = admin.post("/admin/models/claude/refresh", follow_redirects=True)
    html = response.get_data(as_text=True)
    assert "waiting for a fresh subscription report" in html
    assert "Last-known figures are marked stale" in html
    assert "Claude pool quota re-read" not in html


def test_future_observation_is_not_presented_as_last_known(admin, telemetry):
    _, report = telemetry
    report["observed_at"] = time.time() + 100
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert "Last known usage · stale." not in html
    assert "Weekly remaining: 84.0%" not in html
    assert "Subscription usage is unavailable" in html


def test_missing_quota_keeps_verified_plan_without_making_up_percentages(admin, telemetry):
    _, report = telemetry
    report.update(window_left=None, weekly_left=None)
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert "Subscription: Claude Max" in html
    assert "Subscription usage is unavailable" in html
    assert "Last known usage · stale." not in html
    assert "Weekly remaining: 84.0%" not in html


@pytest.mark.parametrize("provider", pool.PROVIDERS)
def test_subscription_readout_requires_admin_authentication(app, telemetry, provider):
    response = app.test_client().get(f"/admin/models/{provider}")
    assert response.status_code in (302, 401, 403)
    assert "Subscription: Claude Max" not in response.get_data(as_text=True)


def test_admin_readout_separates_real_subscription_percentages_from_token_ledger(app, admin, adapter):
    account = accounts.add_account("readout", window_limit=100_000)
    pool.record_usage(account, 13, 7, provider="claude")
    reports = {str(account): observation(window_left=0.23, weekly_left=0.47)}
    pool.register_site_reporter("claude", lambda: {"account_reports": reports})
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert "Local token budget: 20 / 100,000" in html
    assert "Subscription: Claude Max" in html
    assert "5-hour remaining: 23.0%" in html and "Weekly remaining: 47.0%" in html
    assert "Subscription percentages are separate from local token usage" in html


# ----- connection and models --------------------------------------------------------------------------------

@pytest.mark.parametrize("provider,setting,manifest", [
    ("claude", "BC_CLAUDE_EXTENSION=bananachat.services.claude_code", "BC_CLAUDE_CODE_CONFIG"),
    ("codex", "BC_CODEX_EXTENSION=bananachat.services.codex_cli", "BC_CODEX_CLI_CONFIG"),
])
def test_admin_page_explains_disconnected_adapter_and_only_offers_discovered_models(app, admin, provider, setting,
                                                                                    manifest):
    html = admin.get(f"/admin/models/{provider}").get_data(as_text=True)
    assert "Disconnected" in html and setting in html and manifest in html
    assert not any(f'value="{item["name"]}"' in html for item in pool.CURATED[provider])
    pool.register_site_chat(provider, answer)
    pool.register_site_discovery(provider, lambda: [descriptor(provider)])
    html = admin.get(f"/admin/models/{provider}").get_data(as_text=True)
    assert "Adapter configured" in html and f'value="{MODEL}"' in html
    assert "Local token budget" not in html  # no configured accounts yet
    app.extensions[f"{provider}_transport"] = {"configured": False, "error": True}
    html = admin.get(f"/admin/models/{provider}").get_data(as_text=True)
    assert "could not initialise" in html and manifest in html
    pool.reset_transport()


def test_admin_empty_selection_persists_and_blank_required_budget_is_refused(app, adapter, admin):
    response = admin.post("/admin/models/claude/sync", {"models": MODEL})
    assert response.status_code == 302 and catalog.get_by_name(MODEL) is not None
    response = admin.post("/admin/models/claude/sync", {})
    assert response.status_code == 302 and site_settings.state_get("claude.selected_models") == []
    pool.sync_catalog("claude", source="background")
    assert catalog.get_by_name(MODEL)["backend_available"] == 0
    admin.post("/admin/models/claude/accounts", {"label": "missing-budget", "window_limit": ""})
    assert accounts.get_by_label("missing-budget") is None
    admin.post("/admin/models/claude/accounts", {"label": "zero-budget", "window_limit": "0"})
    assert accounts.get_by_label("zero-budget")["window_limit"] == 0


def test_admin_can_choose_automatic_enrollment_without_changing_global_catalog_policy(app, admin, adapter):
    from bananachat.services import model_lifecycle

    response = admin.post("/admin/models/claude/sync", {"models": MODEL, "enrollment": "automatic"})
    assert response.status_code == 302 and pool.automatic_enrollment("claude")
    assert catalog.get_by_name(MODEL)["is_rolled_out"] == 1
    assert site_settings.state_get("claude.auto_enroll") is True
    assert model_lifecycle.policy()["enrollment"] == "manual" and not pool.automatic_enrollment("codex")


def test_automatic_enrollment_offers_new_models_without_rechecking_excluded_ones(app, admin):
    discovered = [{"name": name, "family": "sonnet", "reasoning": []} for name in (MODEL, "unchecked-sonnet")]
    pool.register_site_chat("claude", answer)
    pool.register_site_discovery("claude", lambda: discovered)
    pool.sync_catalog("claude", selected=[MODEL], auto_enroll=True, source="admin")
    discovered.append({"name": "new-sonnet", "family": "sonnet", "reasoning": []})
    assert pool.sync_catalog("claude", source="background")["enabled"] == ["new-sonnet"]
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert 'value="new-sonnet" checked' in html
    assert 'value="unchecked-sonnet" checked' not in html
    pool.reset_transport()


def test_both_pools_have_a_tab_in_the_models_area(admin):
    html = admin.get("/admin/models").get_data(as_text=True)
    assert '<a href="/admin/models/claude">Claude</a>' in html
    assert '<a href="/admin/models/codex">Codex</a>' in html
    assert '<a href="/admin/models/codex" aria-current="page">Codex</a>' in admin.get(
        "/admin/models/codex").get_data(as_text=True)


def test_refreshing_asks_only_that_providers_adapter_for_fresh_reports_and_models(site, browser, adapters):
    browser.post("/admin/models/codex/refresh")
    assert ("usage", True) in adapters["codex"].calls
    assert ("usage", True) not in adapters["claude"].calls
    html = browser.post("/admin/models/codex/discover", follow_redirects=True).get_data(as_text=True)
    assert "The adapter reports 1 available Codex models." in html
    assert ("invalidate",) in adapters["codex"].calls and ("invalidate",) not in adapters["claude"].calls
    assert catalog.get_by_name(MODEL) is None  # manual enrolment waits for the administrator's selection


def test_a_failing_usage_refresh_pauses_the_pool_without_repeating_the_adapters_words(site, browser, adapters,
                                                                                       monkeypatch, caplog):
    _admin_log(monkeypatch, caplog)
    adapters["codex"].usage_failure = RuntimeError("upstream said sk-private-detail")
    response = browser.post("/admin/models/codex/refresh", follow_redirects=True)
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "The quota report could not be verified. Codex requests are paused until a valid refresh." in html
    assert "Refreshing codex usage reports failed (RuntimeError)" in caplog.text
    assert "sk-private-detail" not in html and "sk-private-detail" not in caplog.text
    assert pool.pooled_quota("codex")["reporter_error"]


def test_a_discovered_model_that_belongs_to_another_provider_is_refused_with_a_message(site, browser):
    browser.post("/admin/models/claude/sync", {"models": MODEL})
    site_settings.state_set("codex.auto_enroll", True)
    response = browser.post("/admin/models/codex/discover", follow_redirects=True)
    assert response.status_code == 200
    assert "A discovered id conflicts with a different model backend." in response.get_data(as_text=True)
    assert catalog.get_by_name(MODEL)["backend"] == "claude"


# ----- the built-in adapters' profiles -----------------------------------------------------------------------

def test_a_profile_serves_one_account_and_the_page_offers_login_checks(site, browser, adapters):
    response = browser.post("/admin/models/claude/accounts", {
        "label": "Subscription one", "profile": "primary", "window_limit": "1000", "priority": "0"})
    assert response.status_code == 302
    row = accounts.get_by_label("Subscription one")
    assert row["provider"] == "claude" and row["window_limit"] == 1000
    assert site_settings.state_get("claude_code_profiles") == {str(row["id"]): "primary"}
    browser.post("/admin/models/claude/accounts", {
        "label": "Duplicate", "profile": "primary", "window_limit": "1000", "priority": "0"})
    assert accounts.get_by_label("Duplicate") is None
    # An account without a binding uses the profile named like it.
    accounts.add_account("secondary", window_limit=1000)
    browser.post("/admin/models/claude/accounts", {"label": "Another", "profile": "secondary", "window_limit": "1000"})
    assert accounts.get_by_label("Another") is None
    html = _page(browser)
    assert "Claude Code configured." in html and "Check login" in html
    assert browser.post(f'/admin/models/claude/accounts/{row["id"]}/check').status_code == 302
    assert ("check", "primary") in adapters["claude"].calls


def test_admin_cannot_rebind_leased_profile_but_can_disable(site, browser):
    account_id = accounts.add_account("primary", window_limit=1000)
    assert accounts.claim(account_id, "test-lease", provider="claude")
    url = f"/admin/models/claude/accounts/{account_id}"
    html = browser.post(url, _form(profile="secondary"), follow_redirects=True).get_data(as_text=True)
    assert "current request to finish before changing its profile" in html
    assert site_settings.state_get("claude_code_profiles", {}) == {}
    assert "Answering a request" in html
    browser.post(url, _form(profile="primary", status="disabled"))
    assert accounts.get(account_id)["status"] == "disabled"
    html = browser.post(url, {"action": "delete"}, follow_redirects=True).get_data(as_text=True)
    assert "This account is answering a request" in html and accounts.get(account_id) is not None
    accounts.release(account_id, "test-lease")


def test_removing_an_account_frees_its_profile(site, browser, adapters):
    browser.post("/admin/models/claude/accounts", {"label": "first", "profile": "primary", "window_limit": "1000"})
    row = accounts.get_by_label("first")
    browser.post(f"/admin/models/claude/accounts/{row['id']}", {"action": "delete"})
    assert accounts.get(row["id"]) is None
    assert site_settings.state_get("claude_code_profiles") == {}
    browser.post("/admin/models/claude/accounts", {"label": "second", "profile": "primary", "window_limit": "1000"})
    assert accounts.get_by_label("second") is not None


@pytest.mark.parametrize("failure,shown,hidden", [
    (None, "Subscription login verified", None),
    (NativeAuthenticationError("This profile needs a subscription login; API-key authentication is not accepted."),
     "API-key authentication is not accepted", None),
    (RuntimeError("provider said sk-private-detail"), "The profile could not be checked", "sk-private-detail"),
])
def test_a_login_check_shows_only_the_adapters_own_messages(site, browser, adapters, monkeypatch, caplog, failure,
                                                            shown, hidden):
    _admin_log(monkeypatch, caplog)
    account_id = accounts.add_account("primary", window_limit=1000)
    adapters["claude"].failure = failure
    html = browser.post(f"/admin/models/claude/accounts/{account_id}/check",
                        follow_redirects=True).get_data(as_text=True)
    assert shown in html
    if hidden:
        assert "Checking a claude profile failed (RuntimeError)" in caplog.text
        assert hidden not in html and hidden not in caplog.text


def test_the_codex_page_shows_only_codex_accounts_with_their_own_plans(site, browser, adapters):
    accounts.add_account("claude-main", provider="claude", window_limit=1000)
    browser.post("/admin/models/codex/accounts", {"label": "codex-main", "profile": "primary", "window_limit": "200k"})
    codex = accounts.get_by_label("codex-main")
    assert codex["provider"] == "codex" and codex["window_limit"] == 200_000
    assert site_settings.state_get("codex_cli_profiles") == {str(codex["id"]): "primary"}
    adapters["codex"].reports = {str(codex["id"]): observation(
        source="codex", subscription_type="plus", window_left=0.4, weekly_left=0.9,
        model_limits={"astra": {"left": 0.25, "resets_at": None}})}
    pool.refresh_quota("codex")
    html = _page(browser, "codex")
    assert "Codex CLI configured." in html and "codex-main" in html and "claude-main" not in html
    assert "Subscription: ChatGPT Plus" in html
    assert "5-hour remaining: 40.0%" in html and "Astra remaining: 25.0%" in html
    assert f'value="{MODEL}"' in html and "<legend class=\"label\">Other GPT</legend>" in html
    html = _page(browser)
    assert "claude-main" in html and "codex-main" not in html and "Astra" not in html
    # The Claude manifest's "primary" is another profile: the providers never share bindings.
    browser.post("/admin/models/claude/accounts", {"label": "claude-two", "profile": "primary", "window_limit": "1k"})
    assert accounts.get_by_label("claude-two")["provider"] == "claude"


def test_a_providers_endpoints_never_change_another_providers_account(site, browser, adapters):
    codex_id = accounts.add_account("codex-only", provider="codex", window_limit=1000)
    assert browser.post(f"/admin/models/claude/accounts/{codex_id}", _form(status="disabled")).status_code == 404
    assert browser.post(f"/admin/models/claude/accounts/{codex_id}", {"action": "delete"}).status_code == 404
    assert browser.post(f"/admin/models/claude/accounts/{codex_id}/check").status_code == 404
    assert accounts.get(codex_id)["status"] == "active"
    assert ("check", "codex-only") not in adapters["claude"].calls


def test_an_adapter_without_profiles_pools_accounts_by_label(make_app, monkeypatch):
    adapter = SimpleNamespace(chat=answer, discover=lambda: [descriptor()],
                              quota=lambda: {"window_left": None, "weekly_left": None})
    app = make_app(CLAUDE_EXTENSION=_install(monkeypatch, adapter, "claude"))
    browser = Browser(app)
    browser.login("admin")
    html = _page(browser)
    assert "Adapter configured." in html and "Use the account labels expected by your adapter" in html
    assert 'name="profile"' not in html and "Check login" not in html
    browser.post("/admin/models/claude/accounts", {"label": "router-1", "window_limit": "1000"})
    row = accounts.get_by_label("router-1")
    assert row is not None and row["provider"] == "claude"
    assert browser.post(f"/admin/models/claude/accounts/{row['id']}/check").status_code == 400
    browser.post(f"/admin/models/claude/accounts/{row['id']}", _form(status="disabled"))
    assert accounts.get(row["id"])["status"] == "disabled"
    pool.reset_transport()


# ----- caps -------------------------------------------------------------------------------------------------

def test_allowance_caps_are_saved_and_shown_on_the_form(site, browser):
    browser.post("/admin/models/claude/accounts", {"label": "capped", "profile": "primary", "window_limit": "100k",
                                                   "window_cap_pct": "70", "weekly_cap_pct": "90"})
    row = accounts.get_by_label("capped")
    assert (row["window_cap_pct"], row["weekly_cap_pct"]) == (70, 90)
    html = _page(browser)
    assert f'id="claude-{row["id"]}-window-cap" name="window_cap_pct" min="1" max="100" step="1" ' \
           'inputmode="numeric" value="70"' in html
    browser.post(f"/admin/models/claude/accounts/{row['id']}", _form(profile="primary", window_cap_pct="50",
                                                                     weekly_cap_pct=""))
    row = accounts.get(row["id"])
    assert (row["window_cap_pct"], row["weekly_cap_pct"]) == (50, None)


@pytest.mark.parametrize("value,problem", [("0", "must be between 1 and 100"), ("101", "must be between 1 and 100"),
                                           ("70.5", "must be a whole number"), ("most", "must be a whole number")])
def test_allowance_caps_that_are_not_whole_percentages_are_refused(site, browser, value, problem):
    account_id = accounts.add_account("primary", window_limit=1000, window_cap_pct=80)
    html = browser.post(f"/admin/models/claude/accounts/{account_id}", _form(profile="primary", weekly_cap_pct=value),
                        follow_redirects=True).get_data(as_text=True)
    assert f"The weekly allowance cap {problem}." in html
    assert accounts.get(account_id)["window_cap_pct"] == 80 and accounts.get(account_id)["weekly_cap_pct"] is None
    browser.post("/admin/models/claude/accounts", {"label": "refused", "profile": "secondary", "window_limit": "1000",
                                                   "window_cap_pct": value})
    assert accounts.get_by_label("refused") is None


def test_family_caps_are_kept_per_family_and_cleared_when_empty(site, browser):
    account_id = accounts.add_account("primary", window_limit=100_000)
    url = f"/admin/models/claude/accounts/{account_id}"
    browser.post(url, _form(profile="primary", cap_pct_fable="60", window_cap_opus="20k", weekly_cap_opus="1.5M"))
    assert accounts.scoped_caps(account_id) == {
        "fable": {"cap_pct": 60, "window_cap": None, "weekly_cap": None},
        "opus": {"cap_pct": None, "window_cap": 20_000, "weekly_cap": 1_500_000}}
    html = _page(browser)
    assert f'id="claude-{account_id}-opus-window" name="window_cap_opus"' in html and 'value="20k"' in html
    assert "<details class=\"inline-details\" open><summary>Model family caps" in html
    browser.post(url, _form(profile="primary", cap_pct_fable="60"))
    assert accounts.scoped_caps(account_id) == {"fable": {"cap_pct": 60, "window_cap": None, "weekly_cap": None}}


@pytest.mark.parametrize("provider,field,value,message", [
    ("claude", "cap_pct_opus", "150", "The Opus allowance cap must be between 1 and 100."),
    ("claude", "window_cap_haiku", "lots", "Haiku 5-hour tokens: write a number of tokens"),
    ("codex", "cap_pct_gpt", "0", "The Other GPT allowance cap must be between 1 and 100."),
    ("codex", "weekly_cap_luna", "2000000000", "Luna weekly tokens must be between 0 and 1,000,000,000 tokens."),
])
def test_a_refused_family_cap_names_its_family_and_changes_nothing(site, browser, provider, field, value, message):
    account_id = accounts.add_account("primary", provider=provider, window_limit=1000)
    accounts.set_scoped_cap(account_id, FAMILIES[provider], cap_pct=40)
    html = browser.post(f"/admin/models/{provider}/accounts/{account_id}",
                        _form(profile="primary", window_limit="5k", **{field: value}),
                        follow_redirects=True).get_data(as_text=True)
    assert message in html
    assert accounts.get(account_id)["window_limit"] == 1000
    assert accounts.scoped_caps(account_id) == {FAMILIES[provider]: {"cap_pct": 40, "window_cap": None,
                                                                     "weekly_cap": None}}


WAITING = "the provider has not freshly reported that allowance"


def test_a_capped_account_without_a_fresh_report_says_it_is_waiting(site, browser, adapters):
    account_id = accounts.add_account("primary", window_limit=100_000, window_cap_pct=70)
    pool.refresh_quota("claude")
    html = _page(browser)
    assert WAITING in html and "Awaiting a fresh report" in html
    assert not pool.usable(accounts.get(account_id))
    adapters["claude"].reports = {str(account_id): observation(window_left=0.6)}
    pool.refresh_quota("claude")
    html = _page(browser)
    assert WAITING not in html and "Awaiting a fresh report" not in html
    assert "5-hour remaining: 60.0% · stops at 70% used" in html
    assert pool.usable(accounts.get(account_id))


def test_a_weekly_cap_on_an_unreported_weekly_allowance_is_explained(site, browser, adapters):
    account_id = accounts.add_account("primary", provider="codex", window_limit=100_000, weekly_cap_pct=80)
    adapters["codex"].reports = {str(account_id): observation(source="codex", subscription_type="plus",
                                                              window_left=0.6, weekly_left=None)}
    pool.refresh_quota("codex")
    html = _page(browser, "codex")
    assert "Weekly remaining: not reported · stops at 80% used" in html
    assert WAITING in html and "Awaiting a fresh report" not in html
    assert not pool.usable(accounts.get(account_id))
    accounts.update_account(account_id, status="disabled")
    assert WAITING not in _page(browser, "codex")  # a disabled account is not waiting for anything
    accounts.update_account(account_id, status="active", weekly_cap_pct=None)
    pool.refresh_quota("codex")
    assert WAITING not in _page(browser, "codex")
    assert pool.usable(accounts.get(account_id))


# ----- recent confirmation ---------------------------------------------------------------------------------

def test_adding_rebinding_and_removing_need_a_confirmation_but_checks_do_not(site, browser, adapters):
    _stale(site)
    html = _page(browser)
    assert 'id="claude-new-reauth"' in html
    browser.post("/admin/models/claude/accounts", {"label": "sneaky", "profile": "primary", "window_limit": "1000"})
    assert accounts.get_by_label("sneaky") is None
    browser.post("/admin/models/claude/accounts", {"label": "confirmed", "profile": "primary", "window_limit": "1000",
                                                   "reauth_secret": ADMIN_PASSWORD})
    row = accounts.get_by_label("confirmed")
    assert row is not None

    _stale(site)
    html = _page(browser)
    assert f'id="claude-{row["id"]}-reauth"' in html and "Needed to change the profile" in html
    assert f'id="claude-{row["id"]}-remove-reauth"' in html
    url = f"/admin/models/claude/accounts/{row['id']}"
    browser.post(url, _form(profile="secondary"))
    assert adapters["claude"].bindings() == {str(row["id"]): "primary"}
    browser.post(url, _form(profile="primary", window_cap_pct="80"))  # budgets and caps are limits, as elsewhere
    assert accounts.get(row["id"])["window_cap_pct"] == 80
    browser.post(url, {"action": "delete"})
    assert accounts.get(row["id"]) is not None
    for action in (f"{url}/check", "/admin/models/claude/refresh", "/admin/models/claude/discover"):
        assert browser.post(action).status_code == 302
    assert ("check", "primary") in adapters["claude"].calls and ("usage", True) in adapters["claude"].calls
    browser.post("/admin/models/claude/sync", {"models": MODEL})
    assert catalog.get_by_name(MODEL) is not None

    browser.post(url, _form(profile="secondary", reauth_secret=ADMIN_PASSWORD))
    assert adapters["claude"].bindings() == {str(row["id"]): "secondary"}
    assert ("invalidate",) in adapters["claude"].calls
    _stale(site)
    browser.post(url, {"action": "delete", "reauth_secret": ADMIN_PASSWORD})
    assert accounts.get(row["id"]) is None
