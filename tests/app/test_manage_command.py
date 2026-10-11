"""Operator commands run on the server (``python -m bananachat.manage``).

``reset-two-step USERNAME`` turns off two-step verification for an account that lost both its
authenticator app and its recovery codes, also when no administrator can sign in any more. On a
managed server ``sudo bananachat reset-two-step USERNAME`` runs it (tests/test_deployment_roles.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.app.conftest import Browser

ROOT = Path(__file__).resolve().parents[2]


def _environ(app) -> dict:
    config = app.config["BC"]
    return {"BC_INSTANCE_DIR": str(config.instance_dir), "BC_DATABASE_PATH": str(config.database_path)}


def _run(app, capsys, *arguments, environ=None):
    from bananachat import manage

    code = manage.main(list(arguments), environ=environ if environ is not None else _environ(app))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _two_step_on(app, username):
    from bananachat import totp
    from bananachat.db import users
    from bananachat.services import two_step

    with app.app_context():
        user = users.get_by_username(username)
        assert two_step.enable(user["id"], totp.generate_secret(), 0)
        return user


def _operator_entries(app):
    from bananachat import db

    with app.app_context():
        return [row.to_dict() for row in db.query(
            "SELECT actor_id, actor_name, action, target, details FROM audit_log WHERE action LIKE 'operator.%'")]


def test_the_reset_turns_two_step_verification_off_signs_out_and_is_audited(app, capsys):
    from bananachat import db
    from bananachat.db import users
    from bananachat.services import two_step

    admin = _two_step_on(app, "admin")
    with app.app_context():
        users.create_session(admin["id"], 30)
        version = users.get(admin["id"])["session_version"]
    code, out, err = _run(app, capsys, "reset-two-step", "admin")
    assert code == 0, err
    assert json.loads(out) == {"outcome": "reset", "username": "admin", "signed_out": True, "api_keys_revoked": 0}
    with app.app_context():
        assert not two_step.enabled(admin["id"])
        assert db.scalar("SELECT COUNT(*) FROM auth_sessions WHERE user_id=?", (admin["id"],)) == 0
        assert users.get(admin["id"])["session_version"] > version  # previous-release cookies end too
    assert _operator_entries(app) == [{"actor_id": None, "actor_name": "server operator",
                                       "action": "operator.user_two_step_reset", "target": "admin",
                                       "details": json.dumps({"api_keys_revoked": 0, "via": "reset-two-step command"})}]
    # The password alone signs in again, and the administrator can set it up anew.
    Browser(app).login("admin", "admin-password")


def test_the_reset_changes_nothing_for_an_account_without_it_or_an_unknown_name(app, make_user, capsys):
    make_user("bob")
    code, out, err = _run(app, capsys, "reset-two-step", "bob")
    assert code == 0, err
    assert json.loads(out)["outcome"] == "unchanged"
    code, out, err = _run(app, capsys, "reset-two-step", "nobody")
    assert code == 1 and out == ""
    assert "No account is named 'nobody'" in err
    assert _operator_entries(app) == []


def test_the_reset_never_creates_or_upgrades_a_database(app, tmp_path, capsys):
    from bananachat import db
    from bananachat.services import two_step

    admin = _two_step_on(app, "admin")
    elsewhere = tmp_path / "elsewhere"
    code, out, err = _run(app, capsys, "reset-two-step", "admin",
                          environ={"BC_INSTANCE_DIR": str(elsewhere), "BC_DATABASE_PATH": str(elsewhere / "chat.db")})
    assert code == 1 and out == "" and "no database" in err
    assert not elsewhere.exists()

    with app.app_context():
        db.execute(f"PRAGMA user_version={db.SCHEMA_VERSION - 1}")
    try:
        code, out, err = _run(app, capsys, "reset-two-step", "admin")
        assert code == 1 and out == "" and "schema version" in err
        with app.app_context():
            assert db.schema_version() == db.SCHEMA_VERSION - 1 and two_step.enabled(admin["id"])
    finally:
        with app.app_context():
            db.execute(f"PRAGMA user_version={db.SCHEMA_VERSION}")
    assert _operator_entries(app) == []


def test_the_command_runs_as_a_module_with_the_service_environment(app):
    """What ``sudo bananachat reset-two-step`` starts as the service account."""
    from bananachat.services import two_step

    admin = _two_step_on(app, "admin")
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("BC_", "BANANA_"))}
    result = subprocess.run([sys.executable, "-m", "bananachat.manage", "reset-two-step", "--", "admin"],
                            capture_output=True, text=True, timeout=120, cwd=ROOT,
                            env={**environment, **_environ(app)})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["outcome"] == "reset"
    with app.app_context():
        assert not two_step.enabled(admin["id"])
