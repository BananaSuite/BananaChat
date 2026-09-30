"""Version 12: a download job remembers which run claimed it.

Additive only, and safe to run again. ``model_pull_jobs.claim_token`` is set
each time the background leader starts a job; what that run writes afterwards
(progress, a retry, a re-queue, the outcome) names the token, so a thread left
over from a previous leader cannot change a job that was claimed again.
Existing rows keep NULL (no run owns them until they are claimed).
"""

from sqlite_migrations import add_columns


def upgrade(conn):
    add_columns(conn, "model_pull_jobs", {"claim_token": "TEXT"})
