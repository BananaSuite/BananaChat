"""Authorization for creating the first administrator."""

import hashlib
import hmac

from flask import current_app, request, session

import config


def setup_authorized():
    if current_app.testing and not current_app.config.get("ENFORCE_SETUP_TOKEN"):
        return True
    expected = hashlib.sha256(config.SETUP_TOKEN.encode("utf-8")).hexdigest()
    return hmac.compare_digest(str(session.get("_setup_authorized", "")), expected)


def authorize_setup():
    if setup_authorized():
        return True
    supplied = request.form.get("setup_token") or request.args.get("setup_token") or ""
    if hmac.compare_digest(supplied.encode("utf-8"), config.SETUP_TOKEN.encode("utf-8")):
        session["_setup_authorized"] = hashlib.sha256(config.SETUP_TOKEN.encode("utf-8")).hexdigest()
        return True
    return False
