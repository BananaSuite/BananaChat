"""Version 20: optional worker offline notice, shown by default.

This only controls notice visibility. Worker health and request
availability continue to be checked independently.
"""

from sqlite_migrations import add_columns


def upgrade(conn):
    add_columns(conn, "site_settings", {
        "worker_offline_warning_enabled": "INTEGER NOT NULL DEFAULT 1 CHECK(worker_offline_warning_enabled IN (0,1))",
    })
