"""Operator commands that work on the database directly: ``python -m bananachat.manage COMMAND``.

* ``reset-two-step USERNAME`` turns off two-step verification for an account
  that lost both its authenticator app and its recovery codes, also when no
  administrator can sign in to do it in Admin → Users. Like that reset it
  ends every session of the account (the person then signs in with the
  password alone and can set it up again) and revokes its API keys (someone
  who held a session may have created one; the person creates new ones), and
  it is recorded in the audit log (``operator.user_two_step_reset``, by
  "server operator").

Run it as the user the service runs as, with the service's environment
(``BC_DATABASE_PATH``, ``BC_INSTANCE_DIR``). On a managed server
``sudo bananachat reset-two-step USERNAME`` does exactly that
(``banana_ops``). The command never creates or upgrades a database: the
database must be at this release's schema version (start the service once
after an update). The result is printed as JSON; the exit status is 0 when the
command ran (``"outcome"``: ``"reset"`` or ``"unchanged"``) and 1 when it was
refused, with the reason on standard error.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from bananachat import db
from bananachat.config import load_config

# The audit log's actor for changes made on the server itself (no account made them).
OPERATOR = {"id": None, "username": "server operator"}


class Refused(Exception):
    """The command cannot run; the message says why and what to do."""


def open_database(config) -> None:
    """Use the service's database as it is: refuse a missing one or one at another schema version."""
    if not config.database_path.is_file():
        raise Refused(f"There is no database at {config.database_path}. Run the command as the service's user "
                      "with the service's environment (on a managed server: sudo bananachat reset-two-step).")
    db.configure(config.database_path)
    if db.scalar("PRAGMA application_id", default=0) != db.APPLICATION_ID:
        raise Refused(f"{config.database_path} is not a BananaChat database.")
    version = db.schema_version()
    if version != db.SCHEMA_VERSION:
        raise Refused(f"The database is at schema version {version} and this release uses {db.SCHEMA_VERSION}. "
                      "Run the command from the release the service runs; after an update, start the service "
                      "once so it upgrades the database.")


def reset_two_step(username: str) -> dict:
    from bananachat.db import tokens, users
    from bananachat.services import two_step

    user = users.get_by_username(username)
    if user is None:
        raise Refused(f"No account is named {username!r}.")
    with db.transaction():
        if not two_step.disable(user["id"]):
            return {"outcome": "unchanged", "username": user["username"],
                    "message": f"{user['username']} does not use two-step verification; nothing was changed."}
        users.revoke_sessions(user["id"])
        revoked = tokens.revoke_all(user["id"])
        users.audit(OPERATOR, "operator.user_two_step_reset", user["username"],
                    {"via": "reset-two-step command", "api_keys_revoked": revoked})
    return {"outcome": "reset", "username": user["username"], "signed_out": True, "api_keys_revoked": revoked}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="python -m bananachat.manage",
                                     description="BananaChat operator commands. Run them as the service's user "
                                                 "with the service's environment.")
    commands = result.add_subparsers(dest="command", required=True)
    reset = commands.add_parser("reset-two-step",
                                help="Turn off two-step verification for an account that lost its authenticator "
                                     "app and its recovery codes (audited; signs the account out everywhere and "
                                     "revokes its API keys)")
    reset.add_argument("username")
    return result


def main(argv=None, *, environ=None) -> int:
    args = parser().parse_args(argv)
    try:
        open_database(load_config(environ, load_secret=False))
        result = reset_two_step(args.username)
    except (Refused, sqlite3.Error) as error:
        print(f"BananaChat: {error}", file=sys.stderr)
        return 1
    finally:
        db.close_thread_connection()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
