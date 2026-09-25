"""Atomic, versioned SQLite migrations for the three application databases.

Migration functions receive a connection with foreign-key enforcement disabled
inside one write transaction. They must not commit, roll back or use SQLite's
``executescript`` (which commits implicitly). Use ``execute_script`` instead.
"""

from contextlib import contextmanager
import re
import sqlite3

from sqlite_runtime import DatabaseUnavailable


def add_columns(connection, table, definitions):
    """Add missing columns from a migration's static schema definitions."""
    names = (table, *definitions)
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in names):
        raise ValueError("Migration table and column names must be SQL identifiers.")
    existing = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')


def execute_script(connection, script):
    """Execute DDL, including triggers, without an implicit transaction commit."""
    statement = []
    for character in script:
        statement.append(character)
        if character == ";" and sqlite3.complete_statement("".join(statement)):
            connection.execute("".join(statement))
            statement.clear()
    remainder = "".join(statement).strip()
    if remainder:
        connection.execute(remainder)


@contextmanager
def schema_transaction(connection):
    """Rebuild tables without cascading deletions; reject invalid results."""
    if connection.in_transaction:
        if connection.execute("PRAGMA foreign_keys").fetchone()[0]:
            raise RuntimeError("Schema rebuilds require a migration transaction.")
        yield
        return
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        violation = connection.execute("PRAGMA foreign_key_check").fetchone()
        if violation:
            raise DatabaseUnavailable(
                "Migration found an invalid foreign-key reference. The upgrade was "
                "rolled back; preserve the database and inspect its integrity before retrying."
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def apply_migrations(connection, application_id, migrations):
    """Apply pending migrations once, recording each version with its data."""
    if connection.in_transaction:
        raise RuntimeError("Migrations must start outside an existing transaction.")
    actual_id = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if actual_id not in (0, application_id):
        raise DatabaseUnavailable("This database belongs to a different BananaSuite application.")
    if not 0 <= version <= len(migrations):
        raise DatabaseUnavailable(
            "This database requires newer application code. Restore a matching code/data "
            "backup to roll back; do not start older code against a newer schema."
        )
    if version and actual_id != application_id:
        raise DatabaseUnavailable("The database schema version has no matching application identity.")
    for index in range(version, len(migrations)):
        with schema_transaction(connection):
            migrations[index](connection)
            connection.execute(f"PRAGMA application_id={int(application_id)}")
            connection.execute(f"PRAGMA user_version={index + 1}")
