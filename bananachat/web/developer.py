"""Developer area: API tokens, credit usage and the API playground.

* ``/developer`` - API keys (created, rotated, renamed, revoked here), the API
  pool's token limits, quick-start snippets and the models usable through the API.
* ``/developer/usage`` - the API-pool usage history.
* ``/developer/playground`` - try models in the browser; answers stream from
  ``POST /developer/playground/send`` through the same pipeline as the API and
  are charged as ``playground`` requests.

A new token is shown exactly once: in the JSON response to the creating
request (displayed in a dialog), or - without JavaScript - on the page that
answers the form. It is never stored in the session or in a flash message.
Old URLs (``/api``, ``/api/usage``, ``/api/playground``) redirect here.
"""

from __future__ import annotations

import json

from flask import Blueprint, Response, current_app, flash, g, jsonify, redirect, render_template, request, \
    stream_with_context, url_for

from bananachat import db, security
from bananachat.db import credits, tokens, users
from bananachat.formatting import tokens_text
from bananachat.i18n import translate
from bananachat.services import inference, limits, status
from bananachat.services.access import AccessContext, usable_models
from bananachat.services.completions import CompletionError, CompletionRun, parse_chat_request

bp = Blueprint("developer", __name__)

BODY_LIMIT = 4 * 1024 * 1024
HISTORY_LIMIT = 200


def _t(key, **params) -> str:
    return translate(g.lang, key, **params)


def _body() -> dict:
    if request.is_json:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    return request.form.to_dict()


# ----- shared page data --------------------------------------------------------

def _meter(label: str, used: float, limit: float) -> dict:
    percent = 100.0 if limit <= 0 else min(100.0, used / limit * 100)
    level = "full" if percent >= 100 else "warn" if percent >= 75 else ""
    return {"label": label, "used": used, "limit": limit, "left": max(0.0, limit - used), "percent": percent,
            "level": level}


def credit_summary(user) -> dict:
    """The API pool's limits for templates: tokens in the 5-hour window and this week, request rate, resets and
    reasons.

    Bonus, tier, dynamic adjustment and grants are included (see ``services.limits.effective``).
    """
    from bananachat.formatting import time_left, time_text

    current = limits.effective(user, "api")
    budget = current.budget()
    window, weekly = current.window, current.weekly
    meters = []
    if not budget.unlimited:
        if window.limited:
            meters.append(_meter(_t("developer.tokens_window"), window.used, window.tokens))
        if weekly.limited:
            meters.append(_meter(_t("developer.tokens_weekly"), weekly.used, weekly.tokens))
    mode, bonus_tokens, _ = current.bonus
    if mode == "multiplier":
        bonus = _t("developer.bonus_multiplier", factor=f"{bonus_tokens:g}")
    elif mode == "fixed":
        bonus_weekly = credits.music_weekly_fixed(g.settings) if weekly.limited and not budget.unlimited else 0
        bonus = _t("developer.bonus_fixed_weekly" if bonus_weekly else "developer.bonus_fixed",
                   tokens=tokens_text(bonus_tokens, g.lang), weekly=tokens_text(bonus_weekly, g.lang))
    else:
        bonus = ""
    notes = []
    if not current.admin:
        seen = set()
        for reason in (*window.reasons, *weekly.reasons, *current.rate.reasons):
            if reason.code.startswith(("grant_", "dynamic_", "tier", "capacity")) and reason not in seen:
                seen.add(reason)
                notes.append(reason.text(g.lang))
    window_note = ""
    if window.limited and not budget.unlimited:
        window_note = _t("developer.window_resets", time=time_text(window.resets_at),
                         left=time_left(window.resets_at)) if window.open else _t("developer.window_not_started")
    rate = current.rate
    return {"unlimited": budget.unlimited, "admin": current.admin, "meters": meters, "available": budget.available,
            "bonus": bonus, "regular_left": budget.regular_left,
            "weekly_exhausted": budget.weekly_exhausted, "window_note": window_note,
            "weekly_limited": weekly.limited and not budget.unlimited,
            "resets_at": db.timestamp(window.resets_at) if window.resets_at else None,
            "weekly_resets_at": db.timestamp(weekly.resets_at) if weekly.limited and weekly.resets_at else None,
            "rate": limits.rate_text(g.lang, rate.rules) if rate.limited else "",
            "notes": notes}


def _api_base() -> str:
    return request.url_root.rstrip("/") + "/v1"


def quickstart_snippets(api_base: str) -> list[tuple[str, str]]:
    """``(title or translation key, code)`` pairs for the overview page."""
    curl = (f"curl {api_base}/chat/completions \\\n"
            '  -H "Authorization: Bearer $BANANACHAT_API_KEY" \\\n'
            '  -H "Content-Type: application/json" \\\n'
            """  -d '{"model": "auto", "messages": [{"role": "user", "content": "Hello!"}]}'""")
    python = ("from openai import OpenAI\n\n"
              f'client = OpenAI(base_url="{api_base}", api_key="bc-...")\n\n'
              "response = client.chat.completions.create(\n"
              '    model="auto",\n'
              '    messages=[{"role": "user", "content": "Hello!"}],\n'
              ")\n"
              "print(response.choices[0].message.content)")
    stream = ("stream = client.chat.completions.create(\n"
              '    model="auto",\n'
              '    messages=[{"role": "user", "content": "Write a haiku about bananas."}],\n'
              "    stream=True,\n"
              ")\n"
              "for chunk in stream:\n"
              '    print(chunk.choices[0].delta.content or "", end="", flush=True)')
    return [("curl", curl), ("developer.snippet_python", python), ("developer.snippet_stream", stream)]


def _models(user, kind: str):
    config = current_app.config["BC"]
    # Administrators also see the models waiting for review, marked, to try them by name.
    return usable_models(AccessContext.load(user), "api", kind=kind, images_enabled=config.images_enabled,
                         unreviewed=True)


# ----- pages ------------------------------------------------------------------------

@bp.get("/developer", endpoint="index")
@security.login_required
def index():
    user = g.user
    rows = tokens.list_for(user["id"])
    return render_template("developer/index.html", tokens=rows, max_tokens=tokens.MAX_PER_USER,
                           summary=credit_summary(user), models=_models(user, "any"), api_base=_api_base(),
                           snippets=quickstart_snippets(_api_base()))


@bp.get("/developer/usage", endpoint="usage")
@security.login_required
def usage():
    user = g.user
    return render_template("developer/usage.html", history=credits.history(user["id"], pool="api",
                                                                            limit=HISTORY_LIMIT),
                           summary=credit_summary(user), limit=HISTORY_LIMIT)


@bp.get("/developer/playground", endpoint="playground")
@security.login_required
def playground():
    user = g.user
    models = _models(user, "text")
    data = {
        "sendUrl": url_for("developer.playground_send"),
        "apiBase": _api_base(),
        "models": [{"id": model["ollama_name"], "name": model["display_name"] + (
                        f" ({_t('developer.model_unreviewed').lower()})" if model["enrollment"] == "new" else ""),
                    "vision": bool(model["supports_vision"]), "reasoning": bool(model["is_reasoning"]),
                    "effort": limits.effort_summary(user, model, lang=g.lang)}
                   for model in models],
    }
    return render_template("developer/playground.html", models=models, summary=credit_summary(user), data=data,
                           paused=status.inference_block() is not None)


# Addresses used by the previous release.
@bp.get("/api")
def legacy_index():
    return redirect(url_for("developer.index"), 301)


@bp.get("/api/usage")
def legacy_usage():
    return redirect(url_for("developer.usage"), 301)


@bp.get("/api/playground")
def legacy_playground():
    return redirect(url_for("developer.playground"), 301)


# ----- tokens ------------------------------------------------------------------------

def _token_json(token_id: int, raw: str, name: str) -> dict:
    return {"id": token_id, "name": name, "prefix": raw[:12], "token": raw}


def _show_new_token(token_id: int, raw: str, name: str, rotated: bool):
    if security.wants_json():
        response = jsonify(_token_json(token_id, raw, name))
        response.status_code = 201
    else:
        response = current_app.make_response(render_template(
            "developer/token_created.html", token=raw, name=name, rotated=rotated, api_base=_api_base()))
    response.headers["Cache-Control"] = "no-store"
    return response


def _problem(message: str, status: int = 400, code: str = "bad_request"):
    if security.wants_json():
        return security.json_error(message, status, code)
    flash(message, "error")
    return redirect(url_for("developer.index"))


@bp.post("/developer/tokens", endpoint="create_token")
@security.login_required
@security.rate_limit("api-tokens", 30, 3600, per_user=True)
def create_token():
    user = g.user
    name = " ".join(str(_body().get("name") or "").split())[:64]
    try:
        token_id, raw = tokens.create(user["id"], name)
    except ValueError:
        return _problem(_t("developer.token_limit", count=tokens.MAX_PER_USER), 400, "token_limit")
    users.audit(user, "api_token.create", str(token_id), {"name": name, "prefix": raw[:12]}, security.client_ip())
    return _show_new_token(token_id, raw, name, rotated=False)


@bp.post("/developer/tokens/<int:token_id>/rotate", endpoint="rotate_token")
@security.login_required
@security.rate_limit("api-tokens", 30, 3600, per_user=True)
def rotate_token(token_id):
    user = g.user
    current = next((row for row in tokens.list_for(user["id"]) if row["id"] == token_id), None)
    try:
        new_id, raw = tokens.rotate(token_id, user["id"])
    except LookupError:
        return _problem(_t("developer.token_missing"), 404, "not_found")
    users.audit(user, "api_token.rotate", str(token_id), {"new_token": new_id, "prefix": raw[:12]},
                security.client_ip())
    return _show_new_token(new_id, raw, current["name"] if current else "", rotated=True)


@bp.post("/developer/tokens/<int:token_id>/rename", endpoint="rename_token")
@security.login_required
def rename_token(token_id):
    user = g.user
    name = " ".join(str(_body().get("name") or "").split())[:64]
    if not name:
        return _problem(_t("developer.token_name_required"), 400, "name_required")
    if not tokens.rename(token_id, user["id"], name):
        return _problem(_t("developer.token_missing"), 404, "not_found")
    if security.wants_json():
        return jsonify({"id": token_id, "name": name})
    flash(_t("developer.token_renamed"), "success")
    return redirect(url_for("developer.index"))


@bp.post("/developer/tokens/<int:token_id>/revoke", endpoint="revoke_token")
@security.login_required
def revoke_token(token_id):
    user = g.user
    if not tokens.revoke(token_id, user["id"]):
        return _problem(_t("developer.token_missing"), 404, "not_found")
    users.audit(user, "api_token.revoke", str(token_id), None, security.client_ip())
    if security.wants_json():
        return jsonify({"id": token_id, "revoked": True})
    flash(_t("developer.token_revoked"), "success")
    return redirect(url_for("developer.index"))


# ----- playground ------------------------------------------------------------------

def _error(error: CompletionError):
    response = security.json_error(error.message, error.status, error.code)
    if error.retry_after:
        response.headers["Retry-After"] = str(error.retry_after)
    return response


def _event(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


@bp.post("/developer/playground/send", endpoint="playground_send")
@security.login_required
@security.body_limit(BODY_LIMIT)
@security.long_request
def playground_send():
    """Stream one playground answer as server-sent events (JSON body in the API's format)."""
    blocked = status.guard()  # maintenance or AI-server outage: only new answers are refused
    if blocked is not None:
        return blocked
    user = g.user
    # The playground shares the API's request rate.
    decision = limits.check_rate(user, "api")
    if not decision.allowed:
        response = security.json_error(
            _t("developer.rate_limited", rate=limits.rule_text(g.lang, decision.rule),
               seconds=decision.retry_after), 429, "rate_limited")
        response.headers["Retry-After"] = str(decision.retry_after)
        return response
    body = request.get_json(silent=True)
    run = None
    try:
        params = parse_chat_request(body)
        run = CompletionRun(user, params, request_type="playground", lang=g.lang)
        # Until queued or answering: a request that fails at once gets a plain JSON error.
        run.prime(until=(inference.Queued, inference.Delta, inference.Finished))
    except CompletionError as error:
        if run is not None:
            run.close()
        return _error(error)
    response = Response(stream_with_context(_playground_events(run)), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"
    return response


def _playground_events(run: CompletionRun):
    try:
        for event in run.events():
            if isinstance(event, inference.Queued):
                yield _event({"type": "queued", "position": event.position})
            elif isinstance(event, inference.Started):
                yield _event({"type": "started", "model": {"id": event.model["ollama_name"],
                                                           "name": event.model["display_name"]},
                              "notice": event.notice})
            elif isinstance(event, inference.Delta):
                yield _event({"type": "reasoning" if event.thinking else "delta", "text": event.text})
            elif isinstance(event, inference.Finished):
                prompt, completion, estimated = run.usage
                yield _event({"type": "done", "finish_reason": run.finish_reason, "tokens_counted": run.tokens_counted,
                              "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                                        "total_tokens": prompt + completion, "estimated": estimated}})
    except CompletionError as error:
        yield _event({"type": "error", "message": error.message, "code": error.code})
    finally:
        run.close()
