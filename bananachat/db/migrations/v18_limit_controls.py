"""Version 18: independent account/model exemptions and reasoning controls.

Existing limits and history are retained. New flags default to enforcing the
existing policy. The effort table retains its rows, indexes and triggers while
its CHECK accepts the explicitly supported Extra tier between High and Max.
"""

import re

from sqlite_migrations import add_columns


def _extra_effort(conn):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='user_effort_levels'").fetchone()
    if not row or not row[0]:
        return
    sql = row[0]
    widened = re.sub(r"(level\s+IN\s*\(\s*'off'\s*,\s*'low'\s*,\s*'medium'\s*,\s*'high')\s*,\s*'max'\s*\)",
                     r"\1, 'extra', 'max')", sql, flags=re.IGNORECASE)
    if widened == sql:
        return
    dependents = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='user_effort_levels' AND type IN ('index','trigger') "
        "AND sql IS NOT NULL")]
    sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='user_effort_levels'").fetchone()
    new_sql = re.sub(r'CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?"?user_effort_levels"?',
                     "CREATE TABLE user_effort_levels_v18", widened, count=1, flags=re.IGNORECASE)
    conn.execute(new_sql)
    columns = ", ".join(f'"{r[1]}"' for r in conn.execute('PRAGMA table_info("user_effort_levels")'))
    conn.execute(f"INSERT INTO user_effort_levels_v18 ({columns}) SELECT {columns} FROM user_effort_levels")
    conn.execute("DROP TABLE user_effort_levels")
    conn.execute("ALTER TABLE user_effort_levels_v18 RENAME TO user_effort_levels")
    if sequence:
        conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name='user_effort_levels'", (sequence[0],))
    for statement in dependents:
        conn.execute(statement)


def upgrade(conn):
    flags = {name: "INTEGER NOT NULL DEFAULT 0 CHECK(" + name + " IN (0,1))"
             for name in ("token_exempt", "rate_exempt")}
    add_columns(conn, "user_limits", {**flags, "effort_auto_unlock_off":
                                      "INTEGER NOT NULL DEFAULT 0 CHECK(effort_auto_unlock_off IN (0,1))"})
    add_columns(conn, "user_model_limits", flags)
    _extra_effort(conn)
