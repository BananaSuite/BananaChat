"""Provider adapter startup is explicit and fails closed across app factories."""

import sys
from functools import partial
from types import ModuleType, SimpleNamespace

import pytest

from bananachat.config import load_config
from bananachat.services import claude_pool


def _install(monkeypatch, factory):
    module = ModuleType("bc_test_claude_adapter")
    module.create_adapter = factory
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module.__name__


def _adapter(calls):
    def chat(account, model, messages, options, *, cancel=None):
        calls.append((account, model, options))
        yield {"text": "Hello", "tokens_in": 2, "tokens_out": 1, "done": True}

    return SimpleNamespace(
        chat=chat,
        discover=lambda: [{"name": "claude-test-sonnet", "reasoning": ["low", "medium", "high", "xhigh"]}],
        quota=lambda: {"window_left": 0.5, "weekly_left": None},
    )


def test_extension_installs_callbacks_without_calling_provider(make_app, monkeypatch):
    calls = []
    adapter = _adapter(calls)
    module_name = _install(monkeypatch, lambda config: adapter)
    app = make_app(CLAUDE_EXTENSION=module_name)
    assert app.extensions["claude_transport"] == {"configured": True, "error": False}
    assert claude_pool._site_chat == adapter.chat
    assert claude_pool._site_discovery == adapter.discover
    assert claude_pool._site_reporter == adapter.quota
    assert calls == []


def test_missing_callback_leaves_all_transport_disconnected(make_app, monkeypatch):
    module_name = _install(monkeypatch, lambda config: SimpleNamespace(chat=_adapter([]).chat))
    app = make_app(CLAUDE_EXTENSION=module_name)
    assert app.extensions["claude_transport"] == {"configured": False, "error": True}
    assert claude_pool._site_chat is None
    assert claude_pool._site_discovery is None
    assert claude_pool._site_reporter is None


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
    assert claude_pool._site_chat is None


@pytest.mark.parametrize("wrapped", [False, True])
def test_async_factory_is_rejected_without_creating_a_coroutine(make_app, monkeypatch, wrapped):
    async def factory(config):
        return _adapter([])

    app = make_app(CLAUDE_EXTENSION=_install(monkeypatch, partial(factory) if wrapped else factory))
    assert app.extensions["claude_transport"] == {"configured": False, "error": True}


def test_factory_error_is_not_logged_with_secrets(make_app, monkeypatch, caplog):
    def failing_factory(config):
        raise RuntimeError("private-provider-token-must-not-appear")

    app = make_app(CLAUDE_EXTENSION=_install(monkeypatch, failing_factory))
    assert app.extensions["claude_transport"]["error"]
    assert "private-provider-token-must-not-appear" not in caplog.text
    assert claude_pool._site_chat is None


def test_new_unconfigured_app_cannot_reuse_previous_adapter(make_app, monkeypatch):
    module_name = _install(monkeypatch, lambda config: _adapter([]))
    make_app(CLAUDE_EXTENSION=module_name)
    assert claude_pool._site_chat is not None
    app = make_app()
    assert app.extensions["claude_transport"] == {"configured": False, "error": False}
    assert claude_pool._site_chat is None


def test_extension_config_accepts_modules_and_rejects_commands(tmp_path):
    common = {"BC_INSTANCE_DIR": str(tmp_path)}
    assert load_config({**common, "BC_CLAUDE_EXTENSION": "operator_extensions.claude"},
                       load_secret=False).claude_extension == "operator_extensions.claude"
    invalid = load_config({**common, "BC_CLAUDE_EXTENSION": "/tmp/router.py --login"}, load_secret=False)
    assert invalid.claude_extension == ""
    assert any("BC_CLAUDE_EXTENSION" in warning for warning in invalid.warnings)
