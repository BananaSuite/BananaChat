"""Focused tests for personalities, attachment normalization, and artifacts."""

import io
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from PIL import Image
from werkzeug.datastructures import FileStorage

from app import app
import db
import config
from services import chat_files, model_access


class PersonalitiesAndFilesTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.original_setup = db.get_site_settings().get("setup_done")
        with db.get_db_context() as conn:
            conn.execute("UPDATE site_settings SET setup_done=1 WHERE id=1")
            conn.commit()
        self.user_id = db.create_user(
            "personality-test-user", "unused", role="user"
        )
        self.admin_id = db.create_user(
            "personality-test-admin", "unused", role="admin"
        )
        db.set_access_policy(
            "custom_personality", 0, "allow_except_denylist", True, self.admin_id
        )

    def tearDown(self):
        db.delete_user(self.user_id)
        db.delete_user(self.admin_id)
        db.set_access_policy(
            "custom_personality", 0, "allow_except_denylist", True, None
        )
        with db.get_db_context() as conn:
            conn.execute(
                "UPDATE site_settings SET setup_done=? WHERE id=1",
                (int(bool(self.original_setup)),),
            )
            conn.commit()

    def _client(self):
        client = app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = self.user_id
        return client

    def test_default_personality_capability_is_available(self):
        policy = db.get_access_policy("custom_personality", 0)
        self.assertEqual(policy["mode"], "allow_except_denylist")
        self.assertTrue(model_access.can_user_use_custom_personalities(self.user_id))

    def test_legacy_access_policy_constraint_is_migrated(self):
        original_path = config.DATABASE_PATH
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = os.path.join(directory, "legacy.db")
            conn = sqlite3.connect(legacy_path)
            conn.executescript("""
                CREATE TABLE model_access_policies (
                    scope TEXT NOT NULL CHECK(scope IN ('uncensored', 'image_generation', 'category', 'model')),
                    resource_id INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'allow_all' CHECK(mode IN ('allow_all', 'deny_except_allowlist', 'allow_except_denylist')),
                    requests_enabled INTEGER NOT NULL DEFAULT 0 CHECK(requests_enabled IN (0, 1)),
                    updated_by TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY(scope, resource_id),
                    CHECK((scope IN ('uncensored', 'image_generation') AND resource_id=0) OR (scope IN ('category', 'model') AND resource_id>0))
                );
                INSERT INTO model_access_policies(scope, resource_id, mode, requests_enabled)
                VALUES ('uncensored', 0, 'deny_except_allowlist', 1);
            """)
            conn.close()
            try:
                config.DATABASE_PATH = legacy_path
                db.init_db()
                with db.get_db_context() as migrated:
                    sql = migrated.execute(
                        "SELECT sql FROM sqlite_master WHERE name='model_access_policies'"
                    ).fetchone()["sql"]
                    self.assertIn("custom_personality", sql)
                    self.assertEqual(
                        migrated.execute(
                            "SELECT mode FROM model_access_policies "
                            "WHERE scope='uncensored' AND resource_id=0"
                        ).fetchone()["mode"],
                        "deny_except_allowlist",
                    )
                    self.assertEqual(migrated.execute("PRAGMA foreign_key_check").fetchall(), [])
                    triggers = {
                        row["name"] for row in migrated.execute(
                            "SELECT name FROM sqlite_master WHERE type='trigger'"
                        )
                    }
                    self.assertIn("cleanup_model_access_after_model_delete", triggers)
                    self.assertIn("cleanup_model_access_after_category_delete", triggers)
                    migrated.execute("INSERT INTO model_categories (name) VALUES ('Migration test')")
                    category_id = migrated.execute("SELECT last_insert_rowid()").fetchone()[0]
                    migrated.execute(
                        "INSERT INTO model_access_policies(scope, resource_id) VALUES ('category', ?)",
                        (category_id,),
                    )
                    migrated.execute("DELETE FROM model_categories WHERE id=?", (category_id,))
                    self.assertIsNone(migrated.execute(
                        "SELECT 1 FROM model_access_policies WHERE scope='category' AND resource_id=?",
                        (category_id,),
                    ).fetchone())
                db.init_db()
            finally:
                config.DATABASE_PATH = original_path

    def test_multiple_personalities_and_admin_moderation(self):
        first = db.create_personality(
            self.user_id, "Editor", "Be concise and correct grammar.", self.user_id
        )
        second = db.create_personality(
            self.user_id, "Tutor", "Explain ideas with examples.", self.user_id
        )
        self.assertEqual(len(db.list_user_personalities(self.user_id)), 2)
        self.assertIsNotNone(model_access.get_usable_personality(self.user_id, first))
        db.set_personality_moderation(first, True, self.admin_id, reason="Review")
        self.assertIsNone(model_access.get_usable_personality(self.user_id, first))
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        db.set_personality_moderation(first, True, self.admin_id, disabled_until=past)
        self.assertIsNotNone(model_access.get_usable_personality(self.user_id, first))
        self.assertIsNotNone(model_access.get_usable_personality(self.user_id, second))

    def test_expired_temporary_denylist_no_longer_blocks_access(self):
        db.set_access_policy(
            "custom_personality", 0, "allow_except_denylist", True, self.admin_id
        )
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        db.add_access_membership(
            "custom_personality", 0, self.user_id, "denylist",
            added_by=self.admin_id, expires_at=future,
        )
        self.assertFalse(model_access.can_user_use_custom_personalities(self.user_id))
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.add_access_membership(
            "custom_personality", 0, self.user_id, "denylist",
            added_by=self.admin_id, expires_at=past,
        )
        self.assertTrue(model_access.can_user_use_custom_personalities(self.user_id))

    def test_text_and_code_files_are_bounded_context_not_executed(self):
        upload = FileStorage(
            stream=io.BytesIO(b"print('hello')\n"), filename="example.py",
            content_type="text/x-python",
        )
        items = chat_files.process_uploads([upload])
        self.assertEqual(items[0]["kind"], "code")
        self.assertEqual(items[0]["extracted_text"], "print('hello')\n")
        messages = [{"id": 1, "role": "user", "content": "Review it"}]
        attachment = dict(items[0], message_id=1)
        context = chat_files.build_model_messages(messages, [attachment])
        self.assertIn("BEGIN UNTRUSTED ATTACHMENT", context[0]["content"])
        self.assertNotIn("images", context[0])

    def test_attachment_context_cap_prioritizes_recent_files(self):
        original = config.CHAT_MAX_CONTEXT_CHARS
        config.CHAT_MAX_CONTEXT_CHARS = 6
        try:
            messages = [
                {"id": 1, "role": "user", "content": "old"},
                {"id": 2, "role": "user", "content": "new"},
            ]
            attachments = [
                {"id": "old", "message_id": 1, "kind": "text", "filename": "old.txt", "extracted_text": "OLDOLD"},
                {"id": "new", "message_id": 2, "kind": "text", "filename": "new.txt", "extracted_text": "NEWNEW"},
            ]
            context = chat_files.build_model_messages(messages, attachments)
            self.assertNotIn("OLDOLD", context[0]["content"])
            self.assertIn("NEWNEW", context[1]["content"])
        finally:
            config.CHAT_MAX_CONTEXT_CHARS = original

    def test_image_is_normalized_for_ollama_vision_messages(self):
        source = io.BytesIO()
        Image.new("RGB", (12, 8), "red").save(source, "PNG")
        upload = FileStorage(stream=io.BytesIO(source.getvalue()), filename="photo.png")
        item = chat_files.process_uploads([upload])[0]
        self.assertEqual(item["kind"], "image")
        self.assertTrue(item["image_data"].startswith(b"\xff\xd8"))
        context = chat_files.build_model_messages(
            [{"id": 1, "role": "user", "content": "Describe"}],
            [dict(item, message_id=1)],
        )
        self.assertEqual(len(context[0]["images"]), 1)

    def test_message_and_attachments_are_persisted_atomically(self):
        session_id = db.create_session(self.user_id)
        item = chat_files.process_uploads([
            FileStorage(stream=io.BytesIO(b"notes"), filename="notes.txt")
        ])[0]
        message_id = db.add_message_with_attachments(
            session_id, "user", "Read this", [item]
        )
        stored = db.list_session_attachments(session_id)
        self.assertEqual(stored[0]["message_id"], message_id)
        self.assertEqual(stored[0]["extracted_text"], "notes")

    def test_generated_pdf_has_valid_container_and_text(self):
        payload = chat_files.render_text_pdf("Hello from BananaChat")
        self.assertTrue(payload.startswith(b"%PDF-1.4"))
        self.assertIn(b"Hello from BananaChat", payload)
        self.assertTrue(payload.rstrip().endswith(b"%%EOF"))

    def test_personality_page_renders_for_authenticated_user(self):
        response = self._client().get("/personalities")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Create Personality", response.data)

    def test_chat_session_personality_selection_is_owner_checked(self):
        session_id = db.create_session(self.user_id)
        personality_id = db.create_personality(
            self.user_id, "Writer", "Use clear prose.", self.user_id
        )
        response = self._client().post(
            f"/chat/{session_id}/personality", json={"personality_id": personality_id}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.get_session(session_id)["personality_id"], personality_id)
        page = self._client().get(f"/chat/{session_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"personality-select-chat", page.data)

    def test_incognito_chat_rejects_attachments_before_inference(self):
        session_id = db.create_session(self.user_id, is_incognito=True)
        response = self._client().post(
            f"/chat/{session_id}/send",
            data={
                "content": "Read this", "model": "auto",
                "files": (io.BytesIO(b"hello"), "note.txt"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"unavailable in no-history", response.data)

    def test_chat_request_limit_is_enforced_before_route_processing(self):
        session_id = db.create_session(self.user_id)
        original_limit = config.CHAT_MAX_REQUEST_BYTES
        config.CHAT_MAX_REQUEST_BYTES = 1024
        try:
            response = self._client().post(
                f"/chat/{session_id}/send",
                data={
                    "content": "oversized", "model": "auto",
                    "files": (io.BytesIO(b"x" * 2048), "large.txt"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 413)
            response.close()
        finally:
            config.CHAT_MAX_REQUEST_BYTES = original_limit

    def test_assistant_pdf_download_checks_ownership(self):
        session_id = db.create_session(self.user_id)
        message_id = db.add_message(session_id, "assistant", "A safe PDF response")
        response = self._client().get(
            f"/chat/{session_id}/messages/{message_id}/pdf"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF-1.4"))
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")

    def test_admin_personality_moderation_page_renders(self):
        client = app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = self.admin_id
        response = client.get("/admin/personalities")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Personality Moderation", response.data)


if __name__ == "__main__":
    unittest.main()
