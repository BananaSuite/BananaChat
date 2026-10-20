"""Subscription adapter startup is explicit, per provider, and fails closed across app factories."""

import sys
from functools import partial
from types import ModuleType, SimpleNamespace

import pytest

from bananachat.config import load_config
from bananachat.services import subscription_pool


def _install(monkeypatch, factory):
    module = ModuleType("bc_test_subscription_adapter")
    module.create_adapter = factory
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module.__name__


def _adapter(calls, *, provider=None):
    def chat(account, model, messages, options, *, cancel=None):
        calls.append((account, model, options))
        yield {"text": "Hello", "tokens_in": 2, "tokens_out": 1, "done": True}

    adapter = SimpleNamespace(
        chat=chat,
        discover=lambda: [{"name": "claude-test-sonnet", "reasoning": ["low", "medium", "high", "xhigh"]}],
        quota=lambda: {"window_left": 0.5, "weekly_left": None},
    )
    if provider is not None:
        adapter.provider = provider
    return adapter


def _slot(provider):
    return subscription_pool._transports.get(provider, {})


@pytest.mark.parametrize("provider", subscription_pool.PROVIDERS)
def test_extension_installs_callbacks_without_calling_provider(make_app, monkeypatch, provider):
    calls = []
    adapter = _adapter(calls)
    app = make_app(**{f"{provider.upper()}_EXTENSION": _install(monkeypatch, lambda config: adapter)})
    assert app.extensions[f"{provider}_transport"] == {"configured": True, "error": False}
    assert _slot(provider)["chat"] == adapter.chat
    assert _slot(provider)["discovery"] == adapter.discover
    assert _slot(provider)["reporter"] == adapter.quota
    assert calls == []


def test_providers_connect_independently(make_app, monkeypatch):
    app = make_app(CLAUDE_EXTENSION=_install(monkeypatch, lambda config: _adapter([])))
    assert subscription_pool.connected("claude") and not subscription_pool.connected("codex")
    assert app.extensions["codex_transport"] == {"configured": False, "error": False}


def test_an_adapter_for_another_provider_is_refused(make_app, monkeypatch):
    # The Claude adapter wired into the Codex setting must not serve Codex accounts.
    app = make_app(CODEX_EXTENSION=_install(monkeypatch, lambda config: _adapter([], provider="claude")))
    assert app.extensions["codex_transport"] == {"configured": False, "error": True}
    assert not subscription_pool.connected("codex")


def test_missing_callback_leaves_all_transport_disconnected(make_app, monkeypatch):
    module_name = _install(monkeypatch, lambda config: SimpleNamespace(chat=_adapter([]).chat))
    app = make_app(CLAUDE_EXTENSION=module_name)
    assert app.extensions["claude_transport"] == {"configured": False, "error": True}
    assert _slot("claude") == {}


@pytest.mark.parametrize("requires_cancel", [False, True])
def test_extension_requires_a_cancellable_chat_callback(make_app, monkeypatch, requires_cancel):
    def cancellable(account, model, messages, options, *, cancel):
        return iter(())

    def legacy(account, model, messages, options):
        return iter(())

    adapter = _adapter([])
    adapter.chat = cancellable if requires_cancel else legacy
    app = make_app(CLAUDE_EXTENSION=_install(monkeypatch, lambda config: adapter))
    assert app.extensions["claude_transport"]["configured"] is requires_cancel
    assert app.extensions["claude_transport"]["error"] is not requires_cancel


@pytest.mark.parametrize("method", ["chat", "discover", "quota"])
@pytest.mark.parametrize("kind", ["function", "object", "partial"])
def test_async_callbacks_fail_closed(make_app, monkeypatch, method, kind):
    async def callback(*args, **kwargs):
        return None

    class AsyncCallable:
        async def __call__(self, *args, **kwargs):
            return None

    adapter = _adapter([])
    setattr(adapter, method, {"function": callback, "object": AsyncCallable(),
                              "partial": partial(callback)}[kind])
    app = make_app(CLAUDE_EXTENSION=_install(monkeypatch, lambda config: adapter))
    assert app.extensions["claude_transport"] == {"configured": False, "error": True}
    assert not subscription_pool.connected("claude")


@pytest.mark.parametrize("wrapped", [False, True])
def test_async_factory_is_rejected_without_creating_a_coroutine(make_app, monkeypatch, wrapped):
    async def factory(config):
        return _adapter([])

    app = make_app(CLAUDE_EXTENSION=_install(monkeypatch, partial(factory) if wrapped else factory))
    assert app.extensions["claude_transport"] == {"configured": False, "error": True}


def test_factory_error_is_not_logged_with_secrets(make_app, monkeypatch, warnings_log):
    def failing_factory(config):
        raise RuntimeError("private-provider-token-must-not-appear")

    app = make_app(CLAUDE_EXTENSION=_install(monkeypatch, failing_factory))
    assert app.extensions["claude_transport"]["error"]
    assert "The claude extension failed to initialize (RuntimeError)" in warnings_log.text
    assert "private-provider-token-must-not-appear" not in warnings_log.text
    assert not subscription_pool.connected("claude")


def test_new_unconfigured_app_cannot_reuse_previous_adapter(make_app, monkeypatch):
    make_app(CLAUDE_EXTENSION=_install(monkeypatch, lambda config: _adapter([])))
    assert subscription_pool.connected("claude")
    app = make_app()
    assert app.extensions["claude_transport"] == {"configured": False, "error": False}
    assert not subscription_pool.connected("claude")


@pytest.mark.parametrize("provider", subscription_pool.PROVIDERS)
def test_extension_config_accepts_modules_and_rejects_commands(tmp_path, provider):
    name = f"BC_{provider.upper()}_EXTENSION"
    common = {"BC_INSTANCE_DIR": str(tmp_path)}
    config = load_config({**common, name: f"operator_extensions.{provider}"}, load_secret=False)
    assert getattr(config, f"{provider}_extension") == f"operator_extensions.{provider}"
    invalid = load_config({**common, name: "/tmp/router.py --login"}, load_secret=False)
    assert getattr(invalid, f"{provider}_extension") == ""
    assert any(name in warning for warning in invalid.warnings)
