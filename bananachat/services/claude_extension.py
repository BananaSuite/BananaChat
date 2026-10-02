"""Load an explicitly configured, operator-installed Claude provider adapter.

No transport, credentials or network connection is enabled by default. The
adapter is trusted server code, installed separately from user uploads. See
docs/claude-router.md for the provider restrictions and callback contract.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import re

from bananachat.services import claude_pool

log = logging.getLogger("bananachat.claude_extension")


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
    """Install all callbacks together, or leave the provider disconnected.

    Factory and validation errors must not expose credentials in logs or keep
    a previous adapter alive. Initialization performs no provider requests;
    discovery and quota refresh use the background service's normal schedule.
    """
    claude_pool.reset_transport()
    app.extensions.pop("claude_adapter", None)
    state = {"configured": False, "error": False}
    app.extensions["claude_transport"] = state
    module_name = app.config["BC"].claude_extension
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
        chat = _callback(adapter, "chat", {}, "model", [], {}, cancel=None)
        discover = _callback(adapter, "discover")
        quota = _callback(adapter, "quota")
        claude_pool.register_site_chat(chat)
        claude_pool.register_site_discovery(discover)
        claude_pool.register_site_reporter(quota)
    except Exception as error:
        claude_pool.reset_transport()
        state["error"] = True
        # Exception messages can contain secrets from an external adapter.
        log.error("Claude extension initialization failed (%s); provider remains disconnected.",
                  type(error).__name__)
        return
    app.extensions["claude_adapter"] = adapter
    state["configured"] = True
