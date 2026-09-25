"""Focused database and service tests for centralized model authorization."""

import os
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest import mock
from concurrent.futures import ThreadPoolExecutor

from flask import Flask


_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("BC_ENV", "testing")
os.environ.setdefault("BC_INSTANCE_DIR", _tmp.name)
os.environ.setdefault("BC_DATABASE_PATH", os.path.join(_tmp.name, "model-access.db"))
os.environ.setdefault("BC_LOGGING_LEVEL", "off")

import db  # noqa: E402
import config  # noqa: E402
from services import model_access  # noqa: E402
from services import image_generation, ollama  # noqa: E402
import routes.api_v1 as api_routes  # noqa: E402


db.init_db()


class ModelAccessTests(unittest.TestCase):
    def setUp(self):
        self.previous_image_backend = config.IMAGE_BACKEND
        config.IMAGE_BACKEND = "comfyui"
        with db.get_db_context() as conn:
            conn.execute("DELETE FROM model_access_requests")
            conn.execute("DELETE FROM model_access_memberships")
            conn.execute("DELETE FROM model_access_policies WHERE scope IN ('category', 'model')")
            conn.execute("DELETE FROM model_category_assignments")
            conn.execute("DELETE FROM model_categories")
            conn.execute("DELETE FROM ai_models")
            conn.execute("DELETE FROM users")
            conn.execute(
                "UPDATE model_access_policies SET mode='deny_except_allowlist', "
                "requests_enabled=1, updated_by=NULL WHERE scope='uncensored'"
            )
            conn.execute(
                "UPDATE model_access_policies SET mode='allow_all', "
                "requests_enabled=1, updated_by=NULL WHERE scope='image_generation'"
            )
            conn.executemany(
                "INSERT INTO users(id, username, password, role) VALUES (?,?,?,?)",
                [
                    ("user-1", "user-one", "unused", "user"),
                    ("user-2", "user-two", "unused", "user"),
                    ("admin-1", "admin-one", "unused", "admin"),
                ],
            )
            conn.commit()
        self.user = db.get_user_by_id("user-1")
        self.other_user = db.get_user_by_id("user-2")
        self.admin = db.get_user_by_id("admin-1")

    def tearDown(self):
        config.IMAGE_BACKEND = self.previous_image_backend

    def _model(self, name, rolled_out=1, uncensored=0, image=0):
        backend = "comfyui" if image else "ollama"
        backend_name = name
        public_name = f"comfyui:{name}" if image else name
        with db.get_db_context() as conn:
            cur = conn.execute(
                "INSERT INTO ai_models "
                "(ollama_name, backend, backend_model_name, backend_available, "
                "display_name, is_rolled_out, is_uncensored, is_image_generation) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (public_name, backend, backend_name, 1, name.title(), rolled_out, uncensored, image),
            )
            conn.commit()
            return db.get_model_by_id(cur.lastrowid)

    def test_policy_mode_truth_table_and_lists_coexist(self):
        model = self._model("truth")
        key = ("model", model["id"], self.user["id"])
        db.add_access_membership(*key, "allowlist", added_by=self.admin["id"])
        db.add_access_membership(*key, "denylist", added_by=self.admin["id"])

        db.set_access_policy("model", model["id"], "allow_all")
        self.assertTrue(db.is_user_allowed_by_policy(*key))
        db.set_access_policy("model", model["id"], "deny_except_allowlist")
        self.assertTrue(db.is_user_allowed_by_policy(*key))
        db.set_access_policy("model", model["id"], "allow_except_denylist")
        self.assertFalse(db.is_user_allowed_by_policy(*key))

        memberships = db.list_access_memberships("model", model["id"])
        self.assertEqual({row["list_type"] for row in memberships}, {"allowlist", "denylist"})
        db.remove_access_membership(*key, "denylist")
        self.assertTrue(db.is_user_allowed_by_policy(*key))
        db.set_access_policy("model", model["id"], "deny_except_allowlist")
        db.remove_access_membership(*key, "allowlist")
        self.assertFalse(db.is_user_allowed_by_policy(*key))

    def test_admin_bypasses_rollout_and_all_policy_gates(self):
        model = self._model("admin-only", rolled_out=0, uncensored=1, image=1)
        db.set_access_policy("image_generation", 0, "deny_except_allowlist")
        db.set_access_policy("model", model["id"], "deny_except_allowlist")
        self.assertTrue(model_access.can_user_access_model(self.admin, model, "api"))
        self.assertFalse(model_access.can_user_access_model(self.user, model, "api"))
        self.assertFalse(model_access.can_user_access_model(self.admin, 999999, "api"))

    def test_model_access_is_and_across_every_applicable_gate(self):
        model = self._model("gated", uncensored=1, image=1)
        category_id = db.create_category("API restricted", scope="api")
        db.assign_model_category(model["id"], category_id)
        db.set_access_policy("image_generation", 0, "deny_except_allowlist")
        db.set_access_policy("category", category_id, "deny_except_allowlist")
        db.set_access_policy("model", model["id"], "deny_except_allowlist")

        for scope, resource_id in (
            ("uncensored", 0),
            ("image_generation", 0),
            ("model", model["id"]),
        ):
            db.add_access_membership(
                scope, resource_id, self.user["id"], "allowlist", self.admin["id"]
            )
        self.assertFalse(model_access.can_user_access_model(self.user, model, "api"))
        denial_scopes = {
            item["scope"] for item in model_access.get_denial_reasons(
                self.user, model, "api"
            )
        }
        self.assertEqual(denial_scopes, {"category"})

        # The API-only category is not an applicable chat gate.
        self.assertTrue(model_access.can_user_access_model(self.user, model, "chat"))
        # Image-generation models stay out of ordinary text-chat listings.
        self.assertEqual(
            model_access.list_accessible_models(self.user, "chat"), []
        )

        db.add_access_membership(
            "category", category_id, self.user["id"], "allowlist", self.admin["id"]
        )
        self.assertTrue(model_access.can_user_access_model(self.user, model, "api"))

    def test_image_and_uncensored_capability_access_remain_independent(self):
        model = self._model("separate-capabilities", uncensored=1, image=1)
        db.set_access_policy("image_generation", 0, "deny_except_allowlist")

        db.add_access_membership(
            "uncensored", 0, self.user["id"], "allowlist", self.admin["id"]
        )
        self.assertFalse(model_access.can_user_access_model(self.user, model, "api"))
        self.assertEqual(
            {item["scope"] for item in model_access.get_denial_reasons(
                self.user, model, "api"
            )},
            {"image_generation"},
        )

        db.add_access_membership(
            "image_generation", 0, self.user["id"], "allowlist", self.admin["id"]
        )
        self.assertTrue(model_access.can_user_access_model(self.user, model, "api"))

        db.remove_access_membership(
            "uncensored", 0, self.user["id"], "allowlist"
        )
        self.assertFalse(model_access.can_user_access_model(self.user, model, "api"))
        self.assertEqual(
            {item["scope"] for item in model_access.get_denial_reasons(
                self.user, model, "api"
            )},
            {"uncensored"},
        )

    def test_duplicate_request_prevention_and_atomic_approval(self):
        db.set_access_policy("uncensored", 0, "allow_except_denylist", True)
        db.add_access_membership(
            "uncensored", 0, self.user["id"], "denylist", self.admin["id"]
        )
        request_id = db.create_access_request(
            "uncensored", 0, self.user["id"], "Research", True, True
        )
        with self.assertRaisesRegex(ValueError, "pending request"):
            db.create_access_request(
                "uncensored", 0, self.user["id"], "Again", True, True
            )

        resolved = db.resolve_access_request(
            request_id, self.admin["id"], True, "Approved"
        )
        self.assertEqual(resolved["status"], "approved")
        memberships = db.list_access_memberships("uncensored", 0)
        user_lists = {
            row["list_type"] for row in memberships if row["user_id"] == self.user["id"]
        }
        self.assertEqual(user_lists, {"allowlist"})
        self.assertTrue(db.is_user_allowed_by_policy("uncensored", 0, self.user["id"]))
        self.assertEqual(len(db.list_access_requests(user_id=self.user["id"])), 1)

        denied_id = db.create_access_request(
            "uncensored", 0, self.other_user["id"], "Testing", True, True
        )
        db.resolve_access_request(denied_id, self.admin["id"], False)
        other_lists = db.list_access_memberships("uncensored", 0)
        self.assertFalse(any(row["user_id"] == self.other_user["id"] for row in other_lists))

    def test_image_only_listing_requires_image_flag(self):
        text_model = self._model("text")
        image_model = self._model("image", image=1)
        listed = model_access.list_accessible_models(
            self.user, "chat", image_only=True
        )
        self.assertEqual([row["id"] for row in listed], [image_model["id"]])
        self.assertNotEqual(text_model["id"], image_model["id"])

    def test_policy_listing_uses_defaults_counts_and_surface_filter(self):
        chat_category = db.create_category("Chat category", scope="chat")
        db.create_category("API category", scope="api")
        policy = db.get_access_policy("category", chat_category)
        self.assertEqual(policy["mode"], "allow_all")
        self.assertEqual(policy["requests_enabled"], 0)
        self.assertFalse(policy["persisted"])
        db.add_access_membership(
            "category", chat_category, self.user["id"], "allowlist", self.admin["id"]
        )
        policies = db.list_access_policies(scope="category", surface="chat")
        self.assertEqual([row["label"] for row in policies], ["Chat category"])
        self.assertEqual(policies[0]["allowlist_count"], 1)

    def test_auto_selection_never_uses_a_forbidden_model(self):
        restricted = self._model("restricted", uncensored=1)
        public = self._model("public")
        with mock.patch.object(
            ollama,
            "list_available_models",
            return_value=[{"name": "restricted"}, {"name": "public"}],
        ), mock.patch.object(
            ollama, "list_running_models", return_value=[{"name": "restricted"}]
        ):
            selected, error = ollama.select_auto_model(self.user, surface="chat")
        self.assertIsNone(error)
        self.assertEqual(selected["id"], public["id"])
        self.assertNotEqual(selected["id"], restricted["id"])

    def test_image_model_resolution_uses_separate_capability_policy(self):
        model = self._model("image-model", image=1)
        resolved = image_generation.resolve_model(self.user, "comfyui:image-model")
        self.assertEqual(resolved["id"], model["id"])

        db.set_access_policy("image_generation", 0, "deny_except_allowlist")
        with self.assertRaisesRegex(
            image_generation.ImageGenerationError, "not available"
        ):
            image_generation.resolve_model(self.user, "comfyui:image-model")
        self.assertEqual(
            image_generation.resolve_model(self.admin, "comfyui:image-model")["id"],
            model["id"],
        )

    def test_generated_image_payload_is_validated_and_typed(self):
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        normalized = image_generation._normalize_image(png)
        self.assertEqual(normalized["mime_type"], "image/png")
        self.assertEqual(normalized["b64_json"], png)
        with self.assertRaisesRegex(
            image_generation.ImageGenerationError, "malformed"
        ):
            image_generation._normalize_image("not base64")

    def test_ollama_exposes_no_image_generation_method(self):
        self.assertFalse(hasattr(ollama, "generate_images"))

    def test_legacy_catalog_migrates_without_losing_models(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = os.path.join(directory, "legacy.db")
            conn = sqlite3.connect(legacy_path)
            conn.execute(
                "CREATE TABLE ai_models ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ollama_name TEXT NOT NULL UNIQUE, "
                "display_name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', "
                "is_rolled_out INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0, "
                "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            conn.execute(
                "INSERT INTO ai_models(ollama_name, display_name, is_rolled_out) "
                "VALUES ('legacy-model', 'Legacy Model', 1)"
            )
            conn.commit()
            conn.close()

            previous_path = config.DATABASE_PATH
            try:
                config.DATABASE_PATH = legacy_path
                db.init_db()
                db.init_db()
                with db.get_db_context() as migrated:
                    columns = {
                        row["name"] for row in migrated.execute(
                            "PRAGMA table_info(ai_models)"
                        ).fetchall()
                    }
                    model = migrated.execute(
                        "SELECT * FROM ai_models WHERE ollama_name='legacy-model'"
                    ).fetchone()
                    foreign_key_errors = migrated.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()
                self.assertIn("is_uncensored", columns)
                self.assertIn("is_image_generation", columns)
                self.assertIn("backend", columns)
                self.assertIn("backend_model_name", columns)
                self.assertIn("backend_available", columns)
                self.assertIn("backend_last_seen_at", columns)
                self.assertEqual(model["display_name"], "Legacy Model")
                self.assertEqual(model["backend"], "ollama")
                self.assertEqual(model["backend_model_name"], "legacy-model")
                self.assertEqual(model["backend_available"], 1)
                self.assertIsNotNone(model["backend_last_seen_at"])
                self.assertEqual(foreign_key_errors, [])
            finally:
                config.DATABASE_PATH = previous_path

    def test_api_direct_model_submission_cannot_bypass_policy(self):
        self._model("blocked-api-model", uncensored=1)
        api_app = Flask(__name__)
        api_app.register_blueprint(api_routes.api_v1)
        client = api_app.test_client()
        with mock.patch.object(
            api_routes, "_get_token_user",
            return_value=({"id": 1}, self.user),
        ), mock.patch.object(api_routes.db, "touch_token"):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "blocked-api-model",
                    "messages": [{"role": "user", "content": "test"}],
                },
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn("not available", response.get_json()["error"]["message"])

    def test_image_api_returns_openai_base64_shape(self):
        model = self._model("image-api-model", image=1)
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        result = {
            "images": [{"b64_json": png, "mime_type": "image/png"}],
            "tokens_in": 0,
            "tokens_out": 0,
            "wait_ms": 2,
            "duration_ms": 10,
        }
        api_app = Flask(__name__)
        api_app.register_blueprint(api_routes.api_v1)
        client = api_app.test_client()
        with mock.patch.object(
            api_routes, "_get_token_user",
            return_value=({"id": 1}, self.admin),
        ), mock.patch.object(api_routes.db, "touch_token"), \
             mock.patch.object(
                 api_routes.image_generation, "resolve_model", return_value=dict(model)
             ), mock.patch.object(
                 api_routes.image_generation, "generate", return_value=result
             ) as generate, mock.patch.object(api_routes.db, "deduct_credits"), \
             mock.patch.object(api_routes.db, "record_request_metric"), \
             mock.patch.object(api_routes, "log_action"):
            response = client.post(
                "/v1/images/generations",
                json={
                    "model": "comfyui:image-api-model",
                    "prompt": "A banana",
                    "size": "512x512",
                    "response_format": "b64_json",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"], [{"b64_json": png}])
        generate.assert_called_once_with(
            mock.ANY, "A banana", mock.ANY, size="512x512", owner_key=None, authorization_check=mock.ANY
        )

    def test_image_credit_reservation_is_atomic_and_refundable(self):
        model = self._model("reserved-image", image=1)
        db.set_user_quota(self.user["id"], 5, 0, updated_by=self.admin["id"])
        reservation_id, is_slow = db.reserve_image_credits(
            self.user["id"], 5, model_id=model["id"]
        )
        self.assertFalse(is_slow)
        with self.assertRaisesRegex(ValueError, "Not enough credits"):
            db.reserve_image_credits(
                self.user["id"], 5, model_id=model["id"]
            )
        self.assertTrue(
            db.refund_image_credit_reservation(reservation_id, self.user["id"])
        )
        next_id, _ = db.reserve_image_credits(
            self.user["id"], 5, model_id=model["id"]
        )
        self.assertIsNotNone(next_id)
        self.assertTrue(
            db.finalize_image_credit_reservation(next_id, self.user["id"])
        )
        self.assertEqual(db.get_today_usage(self.user["id"])[0], 5)

    def test_invalid_image_size_is_rejected_before_queueing(self):
        model = self._model("invalid-size-image", image=1)
        with mock.patch.object(image_generation.q, "acquire") as acquire:
            with self.assertRaisesRegex(
                image_generation.ImageGenerationError, "width must be between"
            ):
                image_generation.generate(
                    model, "A banana", 1, size="4096x4096"
                )
        acquire.assert_not_called()

    def test_concurrent_image_reservations_cannot_overspend(self):
        model = self._model("concurrent-image", image=1)
        db.set_user_quota(self.user["id"], 5, 0, updated_by=self.admin["id"])

        def reserve_once(_):
            try:
                db.reserve_image_credits(
                    self.user["id"], 5, model_id=model["id"]
                )
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve_once, range(2)))
        self.assertEqual(sorted(results), [False, True])

    def test_abandoned_image_reservation_expires(self):
        model = self._model("stale-image", image=1)
        db.set_user_quota(self.user["id"], 5, 0, updated_by=self.admin["id"])
        stale_id, _ = db.reserve_image_credits(
            self.user["id"], 5, model_id=model["id"]
        )
        with db.get_db_context() as conn:
            conn.execute(
                "UPDATE image_credit_reservations SET "
                "created_at='2000-01-01T00:00:00+00:00', "
                "updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (stale_id,),
            )
            conn.commit()
        fresh_id, _ = db.reserve_image_credits(
            self.user["id"], 5, model_id=model["id"]
        )
        self.assertNotEqual(stale_id, fresh_id)
        with db.get_db_context() as conn:
            stale = conn.execute(
                "SELECT 1 FROM image_credit_reservations WHERE id=?", (stale_id,)
            ).fetchone()
        self.assertIsNone(stale)

    def test_active_image_reservation_uses_updated_at_and_can_be_touched(self):
        self.assertGreater(
            config.IMAGE_CREDIT_RESERVATION_TTL,
            config.COMFYUI_GENERATION_TIMEOUT + 300,
        )
        model = self._model("active-reservation", image=1)
        db.set_user_quota(self.user["id"], 5, 0, updated_by=self.admin["id"])
        reservation_id, _ = db.reserve_image_credits(
            self.user["id"], 5, model_id=model["id"]
        )
        with db.get_db_context() as conn:
            conn.execute(
                "UPDATE image_credit_reservations SET created_at=?, updated_at=? WHERE id=?",
                (
                    "2000-01-01T00:00:00+00:00",
                    "2000-01-01T00:00:00+00:00",
                    reservation_id,
                ),
            )
            conn.commit()
        self.assertTrue(
            db.touch_image_credit_reservation(reservation_id, self.user["id"])
        )
        with self.assertRaises(db.InsufficientImageCreditsError):
            db.reserve_image_credits(self.user["id"], 5, model_id=model["id"])
        self.assertTrue(
            db.finalize_image_credit_reservation(reservation_id, self.user["id"])
        )

    def test_image_generation_heartbeats_while_backend_is_running(self):
        model = dict(self._model("heartbeat-image", image=1))
        heartbeat_calls = []
        slot = mock.MagicMock()
        slot.__enter__.return_value.wait_ms = 0

        def backend(*_args):
            import time
            time.sleep(0.04)
            return b"\x89PNG\r\n\x1a\nimage"

        with mock.patch.object(image_generation.q, "acquire", return_value=slot), \
             mock.patch.object(image_generation.comfyui, "generate_image", side_effect=backend), \
             mock.patch.object(config, "IMAGE_CREDIT_RESERVATION_HEARTBEAT", 0.01):
            image_generation.generate(
                model,
                "banana",
                1,
                size="512x512",
                reservation_heartbeat=lambda: heartbeat_calls.append(True) or True,
            )
        self.assertGreaterEqual(len(heartbeat_calls), 2)

    def test_legacy_image_reservations_gain_updated_at(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = os.path.join(directory, "legacy-reservations.db")
            conn = sqlite3.connect(legacy_path)
            conn.execute(
                "CREATE TABLE image_credit_reservations ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, "
                "token_id INTEGER, model_id INTEGER, credits_reserved REAL NOT NULL, "
                "is_slow INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO image_credit_reservations "
                "(user_id, credits_reserved, created_at) VALUES ('legacy', 1, '2020-01-01')"
            )
            conn.commit()
            conn.close()
            previous_path = config.DATABASE_PATH
            try:
                config.DATABASE_PATH = legacy_path
                db.init_db()
                with db.get_db_context() as migrated:
                    row = migrated.execute(
                        "SELECT created_at, updated_at FROM image_credit_reservations"
                    ).fetchone()
                self.assertEqual(row["updated_at"], row["created_at"])
            finally:
                config.DATABASE_PATH = previous_path

    def test_finalization_failure_is_not_reported_as_insufficient_credit(self):
        model = self._model("billing-failure", image=1)
        result = {
            "images": [], "tokens_in": 0, "tokens_out": 0,
            "wait_ms": 0, "duration_ms": 1,
        }
        api_app = Flask(__name__)
        api_app.register_blueprint(api_routes.api_v1)
        client = api_app.test_client()
        with mock.patch.object(
            api_routes, "_get_token_user", return_value=({"id": 1}, self.user)
        ), mock.patch.object(api_routes.db, "touch_token"), \
             mock.patch.object(
                 api_routes.image_generation, "resolve_model", return_value=dict(model)
             ), mock.patch.object(
                 api_routes.db, "reserve_image_credits", return_value=(123, False)
             ), mock.patch.object(
                 api_routes.image_generation, "generate", return_value=result
             ), mock.patch.object(
                 api_routes.db,
                 "finalize_image_credit_reservation",
                 side_effect=db.ImageCreditReservationError("expired"),
             ):
            response = client.post(
                "/v1/images/generations",
                json={"model": model["ollama_name"], "prompt": "banana"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("credits remaining", response.get_json()["error"]["message"])

    def test_ollama_sync_reconciles_absent_models_only_after_success(self):
        db.sync_ollama_models([
            {"name": "present", "display_name": "Present"},
            {"name": "missing", "display_name": "Missing"},
        ])
        response = {
            "models": [{
                "name": "present",
                "size": 1024,
                "details": {"parameter_size": "1B"},
            }]
        }
        with mock.patch.object(ollama, "_get", return_value=response):
            ollama.sync_models()
        self.assertEqual(db.get_model_by_ollama_name("present")["backend_available"], 1)
        self.assertEqual(db.get_model_by_ollama_name("missing")["backend_available"], 0)

        with mock.patch.object(
            ollama, "_get", side_effect=urllib.error.URLError("offline")
        ):
            with self.assertRaises(ollama.OllamaConnectionError):
                ollama.sync_models()
        self.assertEqual(db.get_model_by_ollama_name("present")["backend_available"], 1)

        with mock.patch.object(ollama, "_get", return_value={"models": [{}]}):
            with self.assertRaises(ollama.OllamaConnectionError):
                ollama.sync_models()
        self.assertEqual(db.get_model_by_ollama_name("present")["backend_available"], 1)

        with mock.patch.object(ollama, "_get", return_value={"models": []}):
            self.assertEqual(ollama.sync_models(), [])
        self.assertEqual(db.get_model_by_ollama_name("present")["backend_available"], 0)

    def test_ollama_sync_rolls_back_catalog_changes_on_conflict(self):
        db.sync_ollama_models([{"name": "existing", "display_name": "Existing"}])
        with db.get_db_context() as conn:
            conn.execute(
                "INSERT INTO ai_models "
                "(ollama_name, backend, backend_model_name, display_name, is_image_generation) "
                "VALUES ('collision', 'comfyui', 'collision', 'Collision', 1)"
            )
            conn.commit()
        with self.assertRaises(ValueError):
            db.sync_ollama_models([
                {"name": "new", "display_name": "New"},
                {"name": "collision", "display_name": "Collision"},
            ])
        self.assertEqual(db.get_model_by_ollama_name("existing")["backend_available"], 1)
        self.assertIsNone(db.get_model_by_ollama_name("new"))


if __name__ == "__main__":
    unittest.main()
