"""BananaChat: Logging module."""

import logging
import threading
from private_logs import PrivateFileHandler
import re

import config


_logger = None
_logger_lock = threading.RLock()
_log_level = None

_LOG_UNSAFE_RE = re.compile(r"[\r\n\x00-\x1f\x7f]")

LOG_OFF = "off"
LOG_MINIMAL = "minimal"
LOG_MEDIUM = "medium"
LOG_VERBOSE = "verbose"
LOG_DEBUG = "debug"

CRITICAL_ACTIONS = {
    "admin_delete_user", "admin_set_role", "setup_complete",
    "admin_migration_export", "admin_migration_import",
    "admin_set_access_policy", "admin_add_access_membership",
    "admin_remove_access_membership", "admin_approve_access_request",
    "admin_deny_access_request",
    "admin_create_personality", "admin_update_personality",
    "admin_moderate_personality", "admin_delete_personality",
}
IMPORTANT_ACTIONS = {
    "login_success", "login_failed", "signup_success", "logout",
    "change_password", "delete_account",
    "admin_create_user", "admin_suspend", "admin_unsuspend",
    "admin_reset_password", "admin_chat_delete",
    "admin_export_user_gdpr", "admin_export_user_chats",
    "generate_invite_code", "delete_invite_code",
    "update_settings", "admin_approve_quota", "admin_deny_quota",
    "admin_rollout_model", "admin_delete_model", "admin_create_category",
    "create_api_token", "revoke_api_token", "rotate_api_token",
    "chat_send", "chat_incognito_send", "api_request",
    "music_opt_in", "music_opt_out",
    "admin_music_settings", "admin_music_force_opt_in", "admin_music_force_opt_out",
    "admin_music_opt_in_all", "admin_music_opt_out_all",
    "admin_music_hide", "admin_music_remove",
    "admin_update_quotas",
    "admin_music_upload_track", "admin_music_delete_track",
    "admin_delete_ollama_model",
    "admin_create_worker", "admin_worker_disabled", "admin_worker_enabled",
    "admin_delete_worker",
    "export_gdpr", "export_chats",
    "delete_all_chats", "submit_quota_request",
    "image_generate",
    "submit_access_request",
    "admin_sync_comfyui_models", "admin_queue_checkpoint_pull",
    "admin_cancel_model_pull",
    "create_personality", "update_personality", "delete_personality",
}


def _get_log_level():
    global _log_level
    if _log_level is not None:
        return _log_level
    level = getattr(config, "LOGGING_LEVEL", "verbose").lower()
    if level not in {LOG_OFF, LOG_MINIMAL, LOG_MEDIUM, LOG_VERBOSE, LOG_DEBUG}:
        level = LOG_VERBOSE
    _log_level = level
    return _log_level


def get_logger():
    """Configure this process's logger once, including concurrent first requests."""
    with _logger_lock:
        return _configure_logger()


def _configure_logger():
    global _logger
    if _logger is not None:
        return _logger

    _logger = logging.getLogger("bananachat")
    level = _get_log_level()

    if level == LOG_OFF:
        _logger.setLevel(logging.CRITICAL + 1)
    elif level == LOG_MINIMAL:
        _logger.setLevel(logging.WARNING)
    else:
        _logger.setLevel(logging.DEBUG)

    if level != LOG_OFF:
        fh = PrivateFileHandler(config.LOG_FILE)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(levelname)-8s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        _logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO if level != LOG_OFF else logging.CRITICAL + 1)
    sh.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(sh)

    return _logger


def _sanitize(value):
    return _LOG_UNSAFE_RE.sub("", str(value))


def log_request(request, user=None):
    """Log an incoming HTTP request at debug level only."""
    if _get_log_level() != LOG_DEBUG:
        return
    logger = get_logger()
    ip = _sanitize(request.remote_addr or "unknown")
    method = _sanitize(request.method)
    path = _sanitize(request.path)
    ua = _sanitize(request.headers.get("User-Agent", ""))
    if isinstance(user, str):
        username = _sanitize(user)
    elif user:
        username = _sanitize(user["username"])
    else:
        username = "anonymous"
    logger.debug(
        "HTTP %-6s | %-40s | user=%-20s | ip=%-15s | ua=%.60s",
        method, path, username, ip, ua,
    )


_SENSITIVE_FIELDS = {"password", "current_password", "new_password",
                     "confirm_password", "secret", "token", "session",
                     "prompt", "instructions", "personality"}


def _is_sensitive_field(name):
    lowered = str(name).lower()
    return lowered in _SENSITIVE_FIELDS or any(
        marker in lowered for marker in ("password", "secret", "token", "authorization")
    )


def _should_log_action(action):
    level = _get_log_level()
    if level == LOG_OFF:
        return False
    if level in (LOG_DEBUG, LOG_VERBOSE):
        return True
    if level == LOG_MEDIUM:
        return action in CRITICAL_ACTIONS or action in IMPORTANT_ACTIONS
    if level == LOG_MINIMAL:
        return action in CRITICAL_ACTIONS
    return True


def log_action(action, request, user=None, **details):
    """Log a named action; redacts sensitive fields."""
    if not _should_log_action(action):
        return

    logger = get_logger()
    ip = _sanitize(request.remote_addr or "unknown")
    if isinstance(user, str):
        username = _sanitize(user)
    elif user:
        username = _sanitize(user["username"])
    else:
        username = "anonymous"

    safe_details = {
        k: ("***" if _is_sensitive_field(k) else _sanitize(v))
        for k, v in details.items()
    }
    category = (
        "CRITICAL" if action in CRITICAL_ACTIONS else
        "IMPORTANT" if action in IMPORTANT_ACTIONS else
        "OTHER"
    )
    detail_parts = [f"{k}={str(v)[:100]}" for k, v in safe_details.items()]
    detail_str = " ".join(detail_parts)

    if detail_str:
        logger.info(
            "[%-9s] %-35s | user=%-20s | ip=%-15s | %s",
            category, _sanitize(action), username, ip, detail_str,
        )
    else:
        logger.info(
            "[%-9s] %-35s | user=%-20s | ip=%-15s",
            category, _sanitize(action), username, ip,
        )
