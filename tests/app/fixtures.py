"""Fixtures for BananaChat application tests.

* ``fake_ollama`` - an imitation Ollama server (see fake_ollama.py).
* ``make_app(**env)`` - build an app on a fresh instance directory. Keyword
  arguments become ``BC_*`` environment values (``BC_`` prefix optional).
* ``app`` / ``client`` - a ready app with setup completed and an ``admin``
  account (password ``admin-password``), and a :class:`Browser` for it.
* ``make_user(username, role="user")`` - create an account (password
  ``<username>-password``).
* ``warnings_log`` - ``caplog`` attached to the ``bananachat`` logger, which
  does not propagate to the root logger.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.app.fake_ollama import FakeOllama  # noqa: E402

TEST_CSRF = "test-csrf-token"


# ----- making applications fast (tests only) -----------------------------------------------------------
# Most tests make an application, and two steps took most of that time: Werkzeug compiling a URL builder
# for each of the ~320 routes, and running every migration on the new, empty database.

def _cache_url_builders() -> None:
    """Compile each URL rule's builder once per test process, not once per application.

    The generated code receives the rule as an argument and depends only on the
    rule's parsed form and defaults, so applications with the same routes can share it.
    """
    from werkzeug.routing import rules

    original = rules.Rule._compile_builder
    compiled: dict = {}

    def cached(self, append_unknown: bool = True):
        try:
            key = (self.rule, self.subdomain, self.host, tuple(self._trace),
                   repr(sorted((self.defaults or {}).items())), append_unknown)
            hash(key)
        except TypeError:
            return original(self, append_unknown)
        builder = compiled.get(key)
        if builder is None:
            builder = compiled[key] = original(self, append_unknown)
        return builder

    rules.Rule._compile_builder = cached


def _cache_migrated_schema() -> None:
    """Copy a database migrated once per test process into every new, empty database.

    Only an empty database (no tables, version 0) gets the copy, and only for the
    same list of migration functions, so upgrade tests and tests that replace a
    migration still run the real migrations.
    """
    import sqlite3
    import threading

    from bananachat import db as database

    original = database.apply_migrations
    templates: dict = {}
    lock = threading.Lock()

    def apply_migrations(connection, application_id, migrations):
        key = (application_id, tuple(map(id, migrations)))
        empty = (connection.execute("PRAGMA user_version").fetchone()[0] == 0
                 and connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0)
        with lock:
            template = templates.get(key) if empty else None
            if template is not None:
                template.backup(connection)
                return
        original(connection, application_id, migrations)
        if empty:
            copy = sqlite3.connect(":memory:", check_same_thread=False)
            connection.backup(copy)
            with lock:
                templates.setdefault(key, copy)

    database.apply_migrations = apply_migrations


_cache_url_builders()
_cache_migrated_schema()

_HASHES: dict = {}


def _hashed(password):
    """The password's hash for the fixture accounts, made once per method and password (salts are not under test)."""
    from bananachat import security

    key = (security._method(), password)
    if key not in _HASHES:
        _HASHES[key] = security.hash_password(password)
    return _HASHES[key]


class Browser:
    """A Flask test client that handles CSRF tokens and sign-in."""

    def __init__(self, app):
        self.app = app
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["csrf"] = TEST_CSRF

    def _ensure_csrf(self):
        # Signing in or out starts a new session; a real browser would load a
        # page and receive a fresh token, the test sets it directly.
        with self.client.session_transaction() as session:
            session["csrf"] = TEST_CSRF

    def get(self, url, **kwargs):
        # Read and close the answer as a browser and the server do: large responses hold a download place until
        # they are closed (security.hold_large_response). buffered=False keeps a response open.
        kwargs.setdefault("buffered", True)
        return self.client.get(url, **kwargs)

    def post(self, url, data=None, **kwargs):
        self._ensure_csrf()
        data = dict(data or {})
        data.setdefault("csrf_token", TEST_CSRF)
        kwargs.setdefault("buffered", True)  # as get(): read and closed, unless a test keeps it open
        return self.client.post(url, data=data, **kwargs)

    def post_json(self, url, payload=None, method="POST", **kwargs):
        self._ensure_csrf()
        headers = {"X-CSRF-Token": TEST_CSRF, "X-Requested-With": "fetch", "Accept": "application/json",
                   **kwargs.pop("headers", {})}
        return self.client.open(url, method=method, json=payload if payload is not None else {}, headers=headers,
                                **kwargs)

    def fetch(self, url, method="GET", **kwargs):
        self._ensure_csrf()
        headers = {"X-CSRF-Token": TEST_CSRF, "X-Requested-With": "fetch", "Accept": "application/json",
                   **kwargs.pop("headers", {})}
        return self.client.open(url, method=method, headers=headers, **kwargs)

    def login(self, username, password=None):
        response = self.post("/login", {"username": username, "password": password or f"{username}-password"})
        assert response.status_code == 302, response.get_data(as_text=True)[:500]
        return response

    def logout(self):
        return self.post("/logout")


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture
def fake_ollama():
    server = FakeOllama().start()
    yield server
    server.stop()


@pytest.fixture
def make_app(tmp_path, fake_ollama):
    created = []

    def factory(*, setup=True, **env):
        from bananachat import create_app
        from bananachat.config import load_config

        environ = {
            "BC_INSTANCE_DIR": str(tmp_path / f"instance{len(created)}"),
            "BC_OLLAMA_URL": fake_ollama.url,
            "BC_ENV": "testing",
            "BC_LOGGING_LEVEL": "minimal",
            "BC_SECRET_KEY": "test-secret-key-that-is-long-enough-0123456789",
            "BC_MIN_FREE_MEMORY_MB": "0",
            "BC_QUEUE_TIMEOUT": "10",
        }
        for key, value in env.items():
            name = key if key.startswith(("BC_", "BANANA_")) else f"BC_{key.upper()}"
            environ[name] = str(value)
        config = load_config(environ)
        app = create_app(config, testing=True)
        created.append(app)
        if setup:
            complete_setup(app)
        return app

    yield factory
    from bananachat import db
    db.close_thread_connection()


def complete_setup(app, username="admin", password="admin-password"):
    from bananachat import db
    from bananachat.db import settings as site_settings
    from bananachat.db import users

    with app.test_request_context():
        with db.transaction():
            users.create(username, _hashed(password), role="admin")
            site_settings.update(setup_done=1, site_name="Test Chat")


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def make_user(app):
    def factory(username, role="user", password=None):
        from bananachat.db import users

        with app.test_request_context():
            user_id = users.create(username, _hashed(password or f"{username}-password"), role=role)
            return users.get(user_id)
    return factory


@pytest.fixture
def client(app):
    return Browser(app)


@pytest.fixture
def admin(client):
    client.login("admin", "admin-password")
    return client


@pytest.fixture
def browser_for(app):
    """Create additional browsers (separate cookie jars) for multi-user tests."""
    def factory():
        return Browser(app)
    return factory


@pytest.fixture
def warnings_log(caplog):
    """The application's logger does not propagate to the root logger, so capture it directly."""
    logger = logging.getLogger("bananachat")
    caplog.set_level(logging.WARNING, logger="bananachat")
    logger.addHandler(caplog.handler)
    yield caplog
    logger.removeHandler(caplog.handler)
