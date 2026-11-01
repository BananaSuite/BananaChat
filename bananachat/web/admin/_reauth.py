"""Recent confirmation before high-impact administrator actions.

Someone who gets hold of an administrator's browser session (a stolen cookie, an
unlocked computer) has every administrator power while that session lasts. The
actions in :data:`ALWAYS` and :data:`WHEN` could keep that power after the
session ends, lock the real administrators out, copy everyone's data or send
users' prompts somewhere new, so they ask the administrator to confirm it is
them: with their password, or with a code from their authenticator app when
two-step verification is on. Signing in counts as a confirmation, and a
confirmation covers that browser session for ``WINDOW_SECONDS``
(``auth_sessions.verified_at``).

Every administrator POST action is classified below; tests/app/test_reauth_coverage.py
fails when one is added without a decision. Forms of the confirmed actions
include ``reauth_field`` (admin/_reauth.html, imported ``with context``); it
renders only while a confirmation is due. A view calls :func:`confirmed` (or
:func:`refusal` for a JSON request) before acting and goes back when it says no.
"""

from __future__ import annotations

from flask import flash, request

from bananachat import security, totp
from bananachat.db import users
from bananachat.services import two_step

from . import bp
from ._helpers import audit

WINDOW_SECONDS = 10 * 60
FIELD = "reauth_secret"
FAILURES = 10  # wrong confirmations per account in FAILURE_WINDOW
FAILURE_WINDOW = 15 * 60

# Always confirmed: endpoint -> why.
ALWAYS = {
    "admin.migration_export": "a copy of every account's data",
    "admin.user_create": "the new account, with a password the session chose, outlives the session",
    "admin.invite_create": "whoever holds the code makes an account that outlives the session",
    "admin.user_password": "takes over the account",
    "admin.user_two_step_reset": "turns off the account's second sign-in step",
    "admin.workers_register": "issues a lasting worker token; the worker reads the prompts routed to it",
    "admin.external_provider_add": "decides where users' prompts, and an API key, are sent",
    "admin.external_provider_save": "decides where users' prompts, and an API key, are sent",
    "admin.external_provider_remove": "deletes the stored API key with the provider",
    "admin.claude_account_add": "adds a subscription that answers users' prompts",
    "admin.codex_account_add": "adds a subscription that answers users' prompts",
    "admin.workers_remote_session": "opens a terminal or a connection to a service on a worker PC",
    "admin.workers_key_create": "issues a lasting key that opens connections to worker PCs",
    "admin.claude_account_login": "signs a subscription in to this server; it then answers users' prompts",
    "admin.codex_account_login": "signs a subscription in to this server; it then answers users' prompts",
}
# Confirmed in one case: endpoint -> when.
WHEN = {
    "admin.user_role": "when the role changes",
    "admin.user_suspend": "when the account is an administrator",
    "admin.user_unsuspend": "when the account is an administrator",
    "admin.user_delete": "when the account is an administrator",
    "admin.settings_save": "when the administrator requirement changes or sign-up opens to everyone",
    "admin.claude_account_save": "when the account's profile changes or the account is removed",
    "admin.codex_account_save": "when the account's profile changes or the account is removed",
}
# Not confirmed: why -> endpoints. Each is audited and within what any administrator session can do anyway.
EXEMPT = {
    "Ends access or stops work; never grants it": (
        "admin.user_session_revoke", "admin.user_sessions_revoke_all", "admin.invite_delete",
        "admin.workers_delete", "admin.agents_stop_all", "admin.agents_task_stop", "admin.models_unload",
        "admin.models_unload_all", "admin.pull_cancel", "admin.pull_pause", "admin.pulls_queue",
        "admin.claude_account_login_cancel", "admin.codex_account_login_cancel", "admin.claude_account_logout",
        "admin.codex_account_logout", "admin.workers_tunnel_close", "admin.workers_key_revoke",
        "admin.workers_command_cancel",
    ),
    # The code completes a sign-in that was confirmed when it started (admin.<provider>_account_login), and
    # only the CLI process waiting for it can redeem it.
    "Completes an action confirmed when it started": (
        "admin.claude_account_login_code",
    ),
    "Moderates what people created; administrators can already read and remove it": (
        "admin.chat_delete", "admin.chat_unshare", "admin.chat_allow_share", "admin.incognito_delete_session",
        "admin.incognito_delete_entry",
        "admin.personality_disable", "admin.personality_enable", "admin.personality_delete",
        "admin.personality_unshare", "admin.agents_task_delete",
    ),
    "Usage limits, quotas and model access: reversible": (
        "admin.limit_tier_create", "admin.limit_tier_update", "admin.limit_tiers_promote", "admin.limit_grant_create",
        "admin.limit_grant_revoke", "admin.limit_model_save", "admin.limit_effort_save", "admin.limit_effort_auto",
        "admin.limit_effort_model", "admin.user_limits_custom", "admin.user_limits_model", "admin.user_limits_effort",
        "admin.user_limits_exemptions", "admin.user_limits_restore", "admin.user_limits_reset",
        "admin.user_limits_speed", "admin.user_limits_tier", "admin.user_limits_dynamic", "admin.quotas_save",
        "admin.quotas_policy", "admin.quotas_community", "admin.quotas_fallback", "admin.quotas_chat_consumption",
        "admin.quotas_reset_usage", "admin.quotas_restore_defaults", "admin.quota_request_resolve",
        "admin.access_policy", "admin.access_member_add", "admin.access_member_remove",
        "admin.access_request_resolve",
    ),
    # Downloads come from the model registry or a checksummed checkpoint; a deleted model can be downloaded
    # again. Pairing the compute server and its tokens are server commands, never web actions.
    "The model catalog and the models on the operator's own servers": (
        "admin.models_sync", "admin.model_rollout", "admin.model_move", "admin.model_edit", "admin.model_remove",
        "admin.model_delete_server", "admin.model_enable", "admin.model_ignore", "admin.model_restore",
        "admin.model_reviewed", "admin.model_lifecycle", "admin.model_settings_save", "admin.model_ignore_rule_add",
        "admin.model_ignore_rule_delete", "admin.pull_create", "admin.pulls_bulk", "admin.pull_checkpoint",
        "admin.pull_move", "admin.pull_retry", "admin.pull_retry_now", "admin.pull_delete", "admin.pulls_clear",
        "admin.model_recovery",
        "admin.category_create", "admin.category_edit", "admin.category_delete", "admin.category_move",
        # On worker PCs only when their owner allows it (the daemon's BC_WORKER_MODEL_MANAGEMENT).
        "admin.workers_command_create", "admin.workers_command_bulk", "admin.workers_command_retry",
    ),
    # Enabling a worker again sends prompts to a token issued (and confirmed) at registration; a worker
    # that cannot be trusted any more is removed, which ends its token. Subscription checks, model lists
    # and quota refreshes use the profile bound (and confirmed) when the account was added or changed.
    "Uses a provider connection, subscription account or worker token that was confirmed when it was saved or issued": (
        "admin.external_provider_test", "admin.external_provider_check", "admin.external_provider_enroll",
        "admin.external_provider_enroll_many", "admin.workers_state", "admin.claude_account_check",
        "admin.claude_sync", "admin.claude_discover", "admin.claude_refresh", "admin.codex_account_check",
        "admin.codex_sync", "admin.codex_discover", "admin.codex_refresh",
        # Asks a worker to listen for sessions sooner; opening one is confirmed (admin.workers_remote_session).
        "admin.workers_remote_arm",
    ),
    # One account at a time, as the user, chat and no-history pages already show it (a copy of every
    # account's data, admin.migration_export, is confirmed).
    "Downloads one account's data": (
        "admin.user_export",
    ),
    # The request records behind the metrics page (who, which model, tokens, times; no prompts or answers).
    "Downloads the request metrics the metrics page shows": (
        "admin.metrics_export",
    ),
    "Site content, appearance and features": (
        "admin.settings_palette_reset", "admin.database_check", "admin.personality_create",
        "admin.personality_featured_create", "admin.personality_featured_update", "admin.personality_publish",
        "admin.agents_settings", "admin.agents_git", "admin.agents_models", "admin.agents_models_refresh",
        "admin.agents_image_check", "admin.music_settings", "admin.music_hide", "admin.music_remove",
        "admin.music_upload", "admin.music_rename", "admin.music_move", "admin.music_delete",
        "admin.music_participant", "admin.music_opt_in_all", "admin.music_opt_out_all",
        "admin.settings_bm_mode_all", "admin.user_bm_mode",
    ),
}


def needed() -> bool:
    """True when this browser session has not signed in or confirmed in the last ten minutes."""
    return not users.session_verified_within(security.current_session_hash(), WINDOW_SECONDS)


@bp.context_processor
def _template_helpers():
    return {"reauth_needed": needed}


def refusal(secret: str | None = None) -> str | None:
    """None when the action may go ahead, otherwise why not (for the administrator).

    *secret* is the password or code sent with the action; by default the form's ``reauth_secret``.
    """
    if not needed():
        return None
    user = security.current_user()
    secret = ((request.form.get(FIELD) if secret is None else secret) or "")
    secret = secret[:1024] if isinstance(secret, str) else ""
    if not secret:
        return ("Confirm it is you first: enter your password, or a code from your authenticator app, with this "
                "change.")
    too_many = "Too many wrong confirmations. Wait a few minutes and try again."
    attempt = users.reserve([(f"reauth-fail:{user['id']}", FAILURES)], FAILURE_WINDOW)
    if attempt is None:
        return too_many
    method = _check(user, secret)
    if method is _TOO_MANY:
        users.release(attempt)
        return too_many
    if method is None:
        audit("reauth_failed", user["username"])
        return "That password or code is not correct. Nothing was changed."
    users.release(attempt)
    users.mark_session_verified(security.current_session_hash())
    audit("reauth", user["username"], {"method": method})
    return None


def confirmed() -> bool:
    """True when the action may go ahead; otherwise a message says why and the caller goes back."""
    problem = refusal()
    if problem:
        flash(problem, "error")
    return problem is None


_TOO_MANY = "too many"


def _check(user, secret: str) -> str | None:
    """``"totp"`` or ``"password"``, None when wrong, :data:`_TOO_MANY` when the account's budget is used up.

    Codes count against the account's two-step budget of the sign-in (per day too), passwords against
    the budget every password confirmation shares (``security.confirm_password``).
    """
    code_attempt = None
    if totp.looks_like_code(secret) and two_step.enabled(user["id"]):
        code_attempt = two_step.reserve_code_attempt(user)
        if code_attempt is None:
            return _TOO_MANY
        if two_step.verify_app_code(user["id"], secret):
            users.release(code_attempt)
            return "totp"
    confirmed = security.confirm_password(user, secret)
    if confirmed is None:
        return _TOO_MANY
    if confirmed:
        users.release(code_attempt)
    return "password" if confirmed else None
