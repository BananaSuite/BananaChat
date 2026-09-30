"""HTTP blueprints. Every area registers here."""

from __future__ import annotations

import importlib
import logging

from flask import Flask

log = logging.getLogger("bananachat")

# (module, blueprint attribute). Order matters only for URL conflicts.
BLUEPRINTS = (
    ("bananachat.web.core", "bp"),
    ("bananachat.web.auth", "bp"),
    ("bananachat.web.chat", "bp"),
    ("bananachat.web.account", "bp"),
    ("bananachat.web.community", "bp"),
    ("bananachat.web.customization", "bp"),
    ("bananachat.web.personalities", "bp"),
    ("bananachat.web.music", "bp"),
    ("bananachat.web.developer", "bp"),
    ("bananachat.web.images", "bp"),
    ("bananachat.web.agents", "bp"),
    ("bananachat.web.api_v1", "bp"),
    ("bananachat.web.worker_api", "bp"),
    ("bananachat.web.admin", "bp"),
)


def register_blueprints(app: Flask) -> None:
    for module_name, attribute in BLUEPRINTS:
        module = importlib.import_module(module_name)
        app.register_blueprint(getattr(module, attribute))
