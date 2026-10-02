"""Administrator interface (English-only by design).

Each section lives in its own module and adds routes to the shared ``admin``
blueprint. Every view must use ``security.admin_required``.
"""

from flask import Blueprint

bp = Blueprint("admin", __name__, url_prefix="/admin")

# Imported for their route registrations.
from . import (  # noqa: E402,F401
    dashboard, users, invites, models, claude, providers, access, personalities, quotas, limits, settings, metrics, audit,
    migration, chats, workers, music, agents,
)
