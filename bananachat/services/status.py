"""Service status: what is wrong right now, and whether new answers can start.

Problems never take the site down. Every page stays reachable (sign-in,
history, account, administration); a banner explains the situation, and only
starting new generations (chat messages, playground, API completions, image
generation) is refused, with a clear 503 answer. Monitors read ``GET /status``.

Conditions, in order of precedence:

* ``maintenance`` - an administrator switched on maintenance mode. Users
  cannot start new answers; administrators still can (to test), and see a
  reminder that the site is closed to everyone else.
* ``outage`` - the inference (compute) server stopped answering and
  ``BC_INFERENCE_OUTAGE_MODE=shutdown``. Answers are paused unless an accessible
  hosted model remains available; then a nonblocking ``local_outage`` is shown.
* ``fallback`` - the primary server is down but ``BC_INFERENCE_OUTAGE_MODE=fallback``
  routes requests to the backup server: informational only.
* ``announcement`` - the administrator's banner message (Settings).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from flask import current_app, g

from bananachat import security
from bananachat.services import health

BLOCKING_KINDS = ("maintenance", "outage")


@dataclass(frozen=True)
class Notice:
    kind: str                 # maintenance | outage | local_outage | fallback | announcement
    level: str                # danger | warning | info
    message: str = ""         # administrator-written text (may be empty)
    blocks_inference: bool = False
    dismissible: bool = False
    since: float | None = None
    admin_bypass: bool = False  # the current user is exempt (administrator in maintenance)
    visible: bool = True       # presentation only; never changes health or inference admission

    def to_dict(self) -> dict:
        return asdict(self)


def _settings() -> dict:
    settings = getattr(g, "settings", None)
    if settings is None:
        from bananachat.db import settings as site_settings
        settings = g.settings = site_settings.get()
    return settings


_CURRENT = object()


def _hosted_models_available(user):
    from bananachat.db import catalog
    from bananachat.services.access import AccessContext, is_text_model
    from bananachat.services import claude_pool

    context = AccessContext.load(user) if user is not None else None
    for model in catalog.list_models(rolled_out_only=True):
        if model["backend"] not in ("claude", "external") or not is_text_model(model):
            continue
        if model["backend"] == "claude" and claude_pool._site_chat is None:
            continue
        if context is None or context.can_use(model, "chat") or context.can_use(model, "api"):
            return True
    return False


def notices(user=_CURRENT) -> list[Notice]:
    """Active notices, most severe first, for *user* (default: the signed-in user; None: the public)."""
    if user is _CURRENT:
        user = security.current_user()
    cache = getattr(g, "_status_notices", {})
    key = user["id"] if user is not None else None
    if key in cache:
        return cache[key]
    config = current_app.config["BC"]
    settings = _settings()
    is_admin = bool(user is not None and user["role"] == "admin")
    result: list[Notice] = []
    if settings.get("maintenance_mode"):
        result.append(Notice("maintenance", "warning", (settings.get("maintenance_message") or "").strip(),
                             blocks_inference=not is_admin, admin_bypass=is_admin))
    if not config.ollama_is_local and health.inference_down():
        since = health.status().get("since")
        visible = bool(settings.get("worker_offline_warning_enabled", 1))
        if config.inference_outage_mode == "shutdown":
            if _hosted_models_available(user):
                result.append(Notice("local_outage", "warning", blocks_inference=False, since=since,
                                     visible=visible))
            else:
                result.append(Notice("outage", "danger", blocks_inference=True, since=since,
                                     visible=visible))
        else:
            result.append(Notice("fallback", "info", since=since, visible=visible))
    if settings.get("warning_banner_enabled") and (settings.get("warning_banner_message") or "").strip():
        result.append(Notice("announcement", "info", settings["warning_banner_message"].strip(),
                             dismissible=bool(settings.get("warning_banner_dismissible"))))
    cache[key] = result
    g._status_notices = cache
    return result


def inference_block(user=_CURRENT) -> Notice | None:
    """The notice that stops *user* from starting a new answer, if any."""
    for notice in notices(user):
        if notice.blocks_inference:
            return notice
    return None


def overall() -> str:
    """``ok``, ``degraded`` (fallback in use), ``maintenance`` or ``outage`` for monitors."""
    kinds = {notice.kind for notice in notices(None)}
    for kind in ("outage", "maintenance"):
        if kind in kinds:
            return kind
    return "degraded" if kinds & {"fallback", "local_outage"} else "ok"


# Messages used for refusals; the interface shows translated text (status.* keys).
REFUSALS = {
    "maintenance": "Maintenance in progress. Sending is paused.",
    "outage": "Model server unavailable. Sending is paused until it reconnects.",
}


def refusal(notice: Notice) -> str:
    text = REFUSALS.get(notice.kind, "Sending is paused.")
    if notice.kind == "maintenance" and notice.message:
        text = f"{text} {notice.message}"
    return text


def refuse_json(notice: Notice, *, openai: bool = False):
    """A 503 answer for a refused generation (``openai=True`` for the /v1 error envelope)."""
    from flask import jsonify

    message = refusal(notice)
    if openai:
        response = jsonify({"error": {"message": message, "type": "server_error", "param": None, "code": notice.kind}})
        response.status_code = 503
    else:
        response = security.json_error(message, 503, notice.kind)
    response.headers["Retry-After"] = "60"
    response.headers["Cache-Control"] = "no-store"
    return response


def guard(user=_CURRENT, *, openai: bool = False):
    """Return a 503 response when *user* may not start a generation now, else None.

    Call it at the top of every view that starts inference::

        blocked = status.guard()
        if blocked:
            return blocked
    """
    notice = inference_block(user)
    return refuse_json(notice, openai=openai) if notice else None
