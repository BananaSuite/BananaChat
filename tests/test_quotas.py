"""Focused tests for quota request policy, decisions, and history."""

import math
import os
import sqlite3
import tempfile
import unittest
from unittest import mock


_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("BC_ENV", "testing")
os.environ.setdefault("BC_INSTANCE_DIR", _tmp.name)
os.environ.setdefault("BC_DATABASE_PATH", os.path.join(_tmp.name, "quotas.db"))
os.environ.setdefault("BC_LOGGING_LEVEL", "off")
os.environ.setdefault("BC_OLLAMA_SYNC_INTERVAL", "3600")
os.environ.setdefault("BC_COMPUTE_SNAPSHOT_INTERVAL", "3600")

from app import app  # noqa: E402
import db  # noqa: E402
import routes.admin as admin_routes  # noqa: E402


class QuotaRequestTests(unittest.TestCase):
    user_id = "quota-user"
    admin_id = "quota-admin"

    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.original_settings = db.get_site_settings()
        with db.get_db_context() as conn:
            conn.execute("DELETE FROM quota_requests")
            conn.execute("DELETE FROM user_quota")
            conn.execute("DELETE FROM users WHERE id IN (?,?)", (self.user_id, self.admin_id))
            conn.executemany(
                "INSERT INTO users(id, username, password, role) VALUES (?,?,?,?)",
                [
                    (self.user_id, "quota-user", "unused", "user"),
                    (self.admin_id, "quota-admin", "unused", "admin"),
                ],
            )
            conn.execute(
                "INSERT INTO user_quota(user_id, daily_credits, daily_slow_credits) "
                "VALUES (?,?,?)",
                (self.user_id, 30, 15),
            )
            conn.execute(
                "UPDATE site_settings SET setup_done=1, maintenance_mode=0, "
                "quota_auto_approve_enabled=0, quota_auto_approve_max_credits=0, "
                "quota_auto_approve_max_slow_credits=0 WHERE id=1"
            )
            conn.commit()

    def tearDown(self):
        with db.get_db_context() as conn:
            conn.execute("DELETE FROM quota_requests")
            conn.execute("DELETE FROM user_quota WHERE user_id=?", (self.user_id,))
            conn.execute("DELETE FROM users WHERE id IN (?,?)", (self.user_id, self.admin_id))
            conn.commit()
        db.update_site_settings(**{
            key: self.original_settings[key]
            for key in (
                "setup_done", "maintenance_mode", "quota_auto_approve_enabled",
                "quota_auto_approve_max_credits",
                "quota_auto_approve_max_slow_credits",
            )
        })

    def _reset_request_and_quota(self):
        with db.get_db_context() as conn:
            conn.execute("DELETE FROM quota_requests")
            conn.execute(
                "UPDATE user_quota SET daily_credits=30, daily_slow_credits=15 "
                "WHERE user_id=?",
                (self.user_id,),
            )
            conn.commit()

    def _client_as(self, user_id):
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
        return client

    def test_auto_approval_enabled_applies_quota_and_persists_reason(self):
        db.update_site_settings(
            quota_auto_approve_enabled=1,
            quota_auto_approve_max_credits=40,
            quota_auto_approve_max_slow_credits=20,
        )

        response = self._client_as(self.user_id).post(
            "/account/quota-request",
            data={
                "new_credits": "40",
                "new_slow_credits": "20",
                "reason": "Larger integration tests",
                "duration_type": "permanent",
            },
            follow_redirects=True,
        )
        outcome = db.list_user_quota_requests(self.user_id)[0]

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Automatically approved", response.data)
        self.assertEqual(outcome["status"], "approved")
        self.assertIsNotNone(outcome["resolved_at"])
        self.assertIsNone(outcome["resolved_by"])
        self.assertEqual(outcome["resolution_source"], "automatic")
        self.assertIn("Automatically approved", outcome["admin_message"])
        quota = db.get_user_quota(self.user_id)
        self.assertEqual((quota["daily_credits"], quota["daily_slow_credits"]), (40, 20))

    def test_disabled_and_over_threshold_requests_remain_manual(self):
        db.update_site_settings(
            quota_auto_approve_enabled=0,
            quota_auto_approve_max_credits=100,
            quota_auto_approve_max_slow_credits=100,
        )
        disabled = db.submit_quota_request(
            self.user_id, 31, 15, "Disabled policy", "permanent"
        )
        self.assertEqual(disabled["status"], "pending")

        self._reset_request_and_quota()
        db.update_site_settings(
            quota_auto_approve_enabled=1,
            quota_auto_approve_max_credits=35,
            quota_auto_approve_max_slow_credits=20,
        )
        over_threshold = db.submit_quota_request(
            self.user_id, 36, 16, "Above regular maximum", "permanent"
        )
        self.assertEqual(over_threshold["status"], "pending")


    def test_one_day_requests_are_rejected_until_expiry_is_supported(self):
        with self.assertRaisesRegex(ValueError, "temporary quota expiry"):
            db.submit_quota_request(
                self.user_id, 31, 16, "Temporary workload", "one_day"
            )
        self.assertEqual(db.list_user_quota_requests(self.user_id), [])

        with db.get_db_context() as conn:
            request_id = conn.execute(
                "INSERT INTO quota_requests "
                "(user_id, new_credits, new_slow_credits, reason, duration_type) "
                "VALUES (?,?,?,?, 'one_day')",
                (self.user_id, 31, 16, "Legacy temporary request"),
            ).lastrowid
            conn.commit()
        with self.assertRaisesRegex(ValueError, "temporary quota expiry"):
            db.resolve_quota_request(request_id, self.admin_id, True)
        self.assertIsNotNone(db.get_pending_quota_request(self.user_id))

    def test_requests_must_be_finite_non_decreasing_real_increases(self):
        for regular, slow in (
            (30, 15),
            (29, 16),
            (31, 14),
            (math.inf, 15),
            (31.5, 15),
        ):
            with self.subTest(regular=regular, slow=slow):
                with self.assertRaises(ValueError):
                    db.submit_quota_request(
                        self.user_id, regular, slow, "Not a genuine increase"
                    )
        self.assertEqual(db.list_user_quota_requests(self.user_id), [])

    def test_partial_unique_index_guards_duplicate_pending_requests(self):
        db.submit_quota_request(self.user_id, 31, 15, "First request")
        with self.assertRaises(sqlite3.IntegrityError):
            with db.get_db_context() as conn:
                conn.execute(
                    "INSERT INTO quota_requests "
                    "(user_id, new_credits, new_slow_credits, reason) VALUES (?,?,?,?)",
                    (self.user_id, 32, 15, "Second request"),
                )

    def test_manual_denial_message_resolver_and_reason_are_visible_and_logged(self):
        pending = db.submit_quota_request(
            self.user_id, 35, 17, "Applicant-visible reason"
        )
        admin_client = self._client_as(self.admin_id)
        with mock.patch.object(admin_routes, "log_action") as log_action:
            response = admin_client.post(
                f"/admin/quota-requests/{pending['id']}/resolve",
                data={"action": "deny", "admin_message": "Capacity is unavailable."},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(log_action.call_args.args[0], "admin_deny_quota")

        page = self._client_as(self.user_id).get("/account")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Applicant-visible reason", page.data)
        self.assertIn(b"Capacity is unavailable.", page.data)
        self.assertIn(b"quota-admin", page.data)
        self.assertIn(b"denied", page.data)

        exported = db.collect_user_gdpr_data(self.user_id)
        self.assertEqual(exported["quota_requests"][0]["admin_message"], "Capacity is unavailable.")
        self.assertEqual(exported["quota_requests"][0]["resolution_source"], "manual")

    def test_malformed_manual_action_does_not_resolve_or_log(self):
        pending = db.submit_quota_request(self.user_id, 35, 17, "Review me")
        client = self._client_as(self.admin_id)
        with mock.patch.object(admin_routes, "log_action") as log_action:
            response = client.post(
                f"/admin/quota-requests/{pending['id']}/resolve",
                data={"action": "not-a-decision", "admin_message": "Must not be used"},
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid quota review action.", response.data)
        self.assertIsNotNone(db.get_pending_quota_request(self.user_id))
        log_action.assert_not_called()

    def test_manual_approval_is_logged_and_applied(self):
        pending = db.submit_quota_request(self.user_id, 35, 17, "Approve me")
        client = self._client_as(self.admin_id)
        with mock.patch.object(admin_routes, "log_action") as log_action:
            response = client.post(
                f"/admin/quota-requests/{pending['id']}/resolve",
                data={"action": "approve", "admin_message": "Approved manually."},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(log_action.call_args.args[0], "admin_approve_quota")
        quota = db.get_user_quota(self.user_id)
        self.assertEqual((quota["daily_credits"], quota["daily_slow_credits"]), (35, 17))


if __name__ == "__main__":
    unittest.main()
