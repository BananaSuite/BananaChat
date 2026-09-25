"""The compute metrics CSV export must survive real snapshot rows.

``get_compute_history`` returns whole database rows, including bookkeeping
columns the export does not publish. A ``DictWriter`` without
``extrasaction`` raises on the first such row, so the page returned 500 for
every deployment that had ever recorded a snapshot.
"""

import csv
import io
import os
import tempfile
import unittest

_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("BC_ENV", "testing")
os.environ.setdefault("BC_INSTANCE_DIR", _tmp.name)
os.environ.setdefault("BC_DATABASE_PATH", os.path.join(_tmp.name, "metrics.db"))
os.environ.setdefault("BC_LOGGING_LEVEL", "off")
os.environ.setdefault("BC_OLLAMA_SYNC_INTERVAL", "3600")
os.environ.setdefault("BC_COMPUTE_SNAPSHOT_INTERVAL", "3600")

from app import app  # noqa: E402
import db  # noqa: E402


class MetricsExportTests(unittest.TestCase):
    admin_id = "metrics-admin"

    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        # The suite shares one database, so remember what to put back: other
        # modules assert on the first-run setup page.
        self.original_setup_done = (db.get_site_settings() or {}).get("setup_done", 0)
        db.update_site_settings(setup_done=1)
        with db.get_db_context() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (self.admin_id,))
            conn.execute(
                "INSERT INTO users(id, username, password, role) VALUES (?,?,?,?)",
                (self.admin_id, "metrics-admin", "unused", "admin"),
            )
            conn.commit()
        db.record_compute_snapshot(
            gpu_name="Test GPU",
            gpu_memory_used_mb=1024,
            gpu_memory_total_mb=8192,
            gpu_utilization_percent=42,
        )

    def tearDown(self):
        db.update_site_settings(setup_done=self.original_setup_done)
        with db.get_db_context() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (self.admin_id,))
            conn.commit()

    def _admin_client(self):
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = self.admin_id
        return client

    def test_export_returns_csv_for_recorded_snapshots(self):
        response = self._admin_client().get("/admin/metrics/export")
        self.assertEqual(response.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        self.assertTrue(rows, "a recorded snapshot should appear in the export")
        self.assertEqual(rows[0]["gpu_name"], "Test GPU")
        self.assertNotIn("id", rows[0], "internal columns must not be published")


if __name__ == "__main__":
    unittest.main()
