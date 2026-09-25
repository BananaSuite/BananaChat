"""Authentication and authorization decorators."""

import functools
import sqlite3

from flask import session, redirect, url_for, flash, g, request, jsonify

import db
from logger import get_logger


def get_current_user():
    """Return the currently logged-in user row, or None."""
    try:
        if hasattr(g, "_current_user"):
            return g._current_user
    except RuntimeError:
        pass

    uid = session.get("user_id")
    try:
        user = db.get_user_by_id(uid) if uid else None
    except sqlite3.OperationalError as exc:
        try:
            get_logger().warning("get_current_user: DB failure, degrading to anonymous: %s", exc)
        except Exception:
            pass
        user = None

    if user and session.get("session_version", 0) != user.get("session_version", 0):
        session.clear()
        user = None

    try:
        g._current_user = user
    except RuntimeError:
        pass
    return user


def login_required(f):
    """Redirect to login if the request has no valid authenticated session."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            next_url = request.path
            if request.query_string:
                next_url += "?" + request.query_string.decode("utf-8")
            if next_url == "/":
                return redirect(url_for("login"))
            return redirect(url_for("login", next=next_url))
        user = get_current_user()
        if not user:
            session.clear()
            flash("Your account was not found. Please log in again.", "error")
            return redirect(url_for("login"))
        if user["suspended"]:
            if db.check_suspension_expired(user["id"]):
                try:
                    if hasattr(g, "_current_user"):
                        del g._current_user
                except RuntimeError:
                    pass
                return f(*args, **kwargs)
            session.clear()
            flash("Your account has been suspended.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Allow only unsuspended admins; return 403 for API requests, redirect for others."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            session.clear()   # remove stale user_id so login_required cleans up too
            if request.is_json:
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login"))
        if user["suspended"] and not db.check_suspension_expired(user["id"]):
            session.clear()
            if request.is_json:
                return jsonify({"error": "Account suspended"}), 403
            flash("Your account has been suspended.", "error")
            return redirect(url_for("login"))
        if user["role"] != "admin":
            if request.is_json:
                return jsonify({"error": "Admin access required"}), 403
            flash("Admin access required.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper
