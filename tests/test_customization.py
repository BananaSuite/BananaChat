"""Per-user customization and site-theme precedence tests."""

import io
import os
import tempfile
import unittest

from PIL import Image


_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("BC_ENV", "testing")
os.environ.setdefault("BC_INSTANCE_DIR", _tmp.name)
os.environ.setdefault("BC_DATABASE_PATH", os.path.join(_tmp.name, "test.db"))
os.environ.setdefault("BC_LOGGING_LEVEL", "off")
os.environ.setdefault("BC_OLLAMA_SYNC_INTERVAL", "3600")
os.environ.setdefault("BC_COMPUTE_SNAPSHOT_INTERVAL", "3600")

from app import app  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402


class CustomizationTests(unittest.TestCase):
    user_id = "themeusr"

    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app.test_client()
        self.original_settings = db.get_site_settings()
        with db.get_db_context() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users(id, username, password, role, accessibility) "
                "VALUES(?,?,?,?,NULL)",
                (self.user_id, "theme-admin", "unused", "admin"),
            )
            conn.execute("UPDATE site_settings SET setup_done=1 WHERE id=1")
            conn.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

    def tearDown(self):
        with db.get_db_context() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (self.user_id,))
            conn.commit()
        restored_keys = {
            "site_name", "signup_mode", "maintenance_mode", "maintenance_message",
            "setup_done", "warning_banner_enabled", "warning_banner_dismissible",
            "warning_banner_message", "default_theme_mode", "primary_color",
            "secondary_color", "accent_color", "text_color", "sidebar_color",
            "bg_color", "light_primary_color", "light_secondary_color",
            "light_accent_color", "light_text_color", "light_sidebar_color",
            "light_bg_color",
        }
        db.update_site_settings(**{
            key: value for key, value in self.original_settings.items()
            if key in restored_keys
        })

    def test_preferences_are_synchronized_and_rendered_on_first_paint(self):
        response = self.client.post("/api/accessibility", json={
            "theme_mode": "light",
            "font_scale": 1.2,
            "contrast": 2,
            "custom_bg": "#123456",
            "custom_text": "not-a-color",
            "semantic_heading": 2,
        })
        self.assertEqual(response.status_code, 200)
        saved = db.get_user_accessibility(self.user_id)
        self.assertEqual(saved["theme_mode"], "light")
        self.assertEqual(saved["font_scale"], 1.2)
        self.assertEqual(saved["custom_bg"], "#123456")
        self.assertEqual(saved["custom_text"], "")

        page = self.client.get("/admin/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'data-theme="light"', page.data)
        self.assertIn(b"--bg: #123456", page.data)
        self.assertIn(b"a11y-contrast-2", page.data)
        self.assertIn(b'id="a11y-panel"', page.data)

    def test_personal_theme_has_priority_over_admin_global_theme(self):
        db.update_site_settings(default_theme_mode="light", light_bg_color="#fefefe")
        following_global = self.client.get("/admin/settings")
        self.assertIn(b'data-theme="light"', following_global.data)
        self.assertIn(b"--bg: #fefefe", following_global.data)

        prefs = db.get_user_accessibility(self.user_id)
        prefs.update({"theme_mode": "dark", "custom_bg": "#010203"})
        db.save_user_accessibility(self.user_id, prefs)
        personal = self.client.get("/admin/settings")
        self.assertIn(b'data-theme="dark"', personal.data)
        self.assertIn(b"--bg: #010203", personal.data)

    def test_admin_can_update_global_palettes(self):
        response = self.client.post("/admin/settings", data={
            "site_name": "BananaChat",
            "signup_mode": "invite",
            "default_theme_mode": "light",
            "primary_color": "#111111",
            "secondary_color": "#222222",
            "accent_color": "#333333",
            "text_color": "#eeeeee",
            "sidebar_color": "#444444",
            "bg_color": "#000000",
            "light_primary_color": "#555555",
            "light_secondary_color": "#ffffff",
            "light_accent_color": "#666666",
            "light_text_color": "#101010",
            "light_sidebar_color": "#eeeeee",
            "light_bg_color": "#fafafa",
        })
        self.assertEqual(response.status_code, 302)
        settings = db.get_site_settings()
        self.assertEqual(settings["default_theme_mode"], "light")
        self.assertEqual(settings["light_bg_color"], "#fafafa")
        public_page = app.test_client().get("/login")
        self.assertEqual(public_page.status_code, 200)
        self.assertIn(b'data-theme="light"', public_page.data)
        self.assertIn(b"--bg: #fafafa", public_page.data)

    def test_background_upload_is_private_to_the_user_and_reset_removes_it(self):
        image = Image.new("RGB", (32, 32), "red")
        payload = io.BytesIO()
        image.save(payload, format="PNG")
        payload.seek(0)
        response = self.client.post(
            "/api/accessibility/background",
            data={"file": (payload, "background.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        filename = response.get_json()["background_image"]
        self.assertRegex(filename, r"^[0-9a-f]{32}\.jpg$")
        self.assertEqual(
            self.client.get(f"/customization/background/{filename}").status_code,
            200,
        )
        self.assertEqual(self.client.post("/api/accessibility/reset", json={}).status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(config.BACKGROUND_UPLOAD_DIR, filename)))


if __name__ == "__main__":
    unittest.main()
