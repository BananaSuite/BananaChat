"""Load explicitly configured, operator-installed subscription adapters.

No transport, credentials or network connection is enabled by default. Each
adapter is trusted server code, installed separately from user uploads. See
docs/subscription-router.md for the provider restrictions and callback contract.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import re

from bananachat.services import subscription_pool

log = logging.getLogger("bananachat.subscription_extension")


def _synchronous(callback) -> bool:
    targets = (callback, callback.__call__)
    return not any(inspect.iscoroutinefunction(target) or inspect.isasyncgenfunction(target)
                   for target in targets)


def _callback(adapter, name, *args, **kwargs):
    callback = getattr(adapter, name, None)
    if not callable(callback) or not _synchronous(callback):
        raise ValueError("Adapter callbacks are missing")
    inspect.signature(callback).bind(*args, **kwargs)
    return callback


def configure(app) -> None:
    """Install each provider's callbacks together, or leave that provider disconnected.

    Factory and validation errors must not expose credentials in logs or keep
    a previous adapter alive. Initialization performs no provider requests;
    discovery and quota refresh use the background service's normal schedule.
    """
    config = app.config["BC"]
    for provider in subscription_pool.PROVIDERS:
        _configure(app, provider, getattr(config, f"{provider}_extension"))


def _configure(app, provider: str, module_name: str) -> None:
    subscription_pool.reset_transport(provider)
    app.extensions.pop(f"{provider}_adapter", None)
    state = {"configured": False, "error": False, "message": "", "module": module_name}
    app.extensions[f"{provider}_transport"] = state
    if not module_name:
        return
    try:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module_name, flags=re.ASCII):
            raise ValueError("Invalid module name")
        module = importlib.import_module(module_name)
        factory = getattr(module, "create_adapter", None)
        if not callable(factory) or not _synchronous(factory):
            raise ValueError("Adapter factory is missing")
        adapter = factory(app.config["BC"])
        # A Claude adapter wired into the Codex setting would serve Codex accounts through the wrong CLI.
        if getattr(adapter, "provider", provider) != provider:
            raise ValueError("Adapter serves another provider")
        chat = _callback(adapter, "chat", {}, "model", [], {}, cancel=None)
        discover = _callback(adapter, "discover")
        quota = _callback(adapter, "quota")
        subscription_pool.register_site_chat(provider, chat)
        subscription_pool.register_site_discovery(provider, discover)
        subscription_pool.register_site_reporter(provider, quota)
    except Exception as error:
        subscription_pool.reset_transport(provider)
        state["error"] = True
        from bananachat.services.subscription_cli import ConnectorError

        if isinstance(error, ConnectorError):
            # The built-in connectors' errors are written for administrators and carry no credentials.
            state["message"] = " ".join(str(error).split())[:600]
        # Exception messages can contain secrets from an external adapter.
        log.error("The %s extension failed to initialize (%s); the provider remains disconnected",
                  provider, type(error).__name__)
        return
    app.extensions[f"{provider}_adapter"] = adapter
    state["configured"] = True
