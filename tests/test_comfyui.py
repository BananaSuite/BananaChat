"""Mocked contract tests for the optional direct ComfyUI backend."""

import os
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest import mock

from flask import Flask


_module_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("BC_ENV", "testing")
os.environ.setdefault("BC_INSTANCE_DIR", _module_tmp.name)
os.environ.setdefault("BC_DATABASE_PATH", os.path.join(_module_tmp.name, "tests.db"))
os.environ.setdefault("BC_LOGGING_LEVEL", "off")

import config  # noqa: E402
import db  # noqa: E402
import routes.api_v1 as api_routes  # noqa: E402
from services import comfyui, image_generation, model_access, ollama  # noqa: E402


def _object_info(checkpoints):
    data = {name: {} for name in comfyui._CORE_NODES}
    data["CheckpointLoaderSimple"] = {
        "input": {"required": {"ckpt_name": [list(checkpoints)]}}
    }
    return data


class ComfyUITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_database = config.DATABASE_PATH
        self.previous_backend = config.IMAGE_BACKEND
        config.DATABASE_PATH = os.path.join(self.directory.name, "comfyui.db")
        config.IMAGE_BACKEND = "comfyui"
        db.init_db()
        with db.get_db_context() as conn:
            conn.executemany(
                "INSERT INTO users(id, username, password, role) VALUES (?,?,?,?)",
                [
                    ("user-1", "user-one", "unused", "user"),
                    ("admin-1", "admin-one", "unused", "admin"),
                ],
            )
            conn.commit()
        self.user = db.get_user_by_id("user-1")
        self.admin = db.get_user_by_id("admin-1")

    def tearDown(self):
        config.DATABASE_PATH = self.previous_database
        config.IMAGE_BACKEND = self.previous_backend
        self.directory.cleanup()

    def test_disabled_backend_makes_no_network_call_and_reserves_no_credits(self):
        config.IMAGE_BACKEND = "disabled"
        with mock.patch.object(comfyui, "_get_json") as get_json:
            self.assertEqual(comfyui.sync_models(), [])
        get_json.assert_not_called()

        app = Flask(__name__)
        app.register_blueprint(api_routes.api_v1)
        client = app.test_client()
        with mock.patch.object(
            api_routes, "_get_token_user", return_value=({"id": 7}, self.user)
        ), mock.patch.object(api_routes.db, "touch_token"), mock.patch.object(
            api_routes.db, "reserve_image_credits"
        ) as reserve:
            response = client.post(
                "/v1/images/generations", json={"prompt": "banana", "model": "auto"}
            )
        self.assertEqual(response.status_code, 503)
        reserve.assert_not_called()

    def test_discovery_sync_preserves_metadata_and_marks_absent_after_success(self):
        with mock.patch.object(
            comfyui, "_get_json", return_value=_object_info(["folder/a.safetensors", "b.ckpt"])
        ):
            models = comfyui.sync_models()
        self.assertEqual(len(models), 2)
        model_a = db.get_model_by_ollama_name("comfyui:folder/a.safetensors")
        model_b = db.get_model_by_ollama_name("comfyui:b.ckpt")
        self.assertEqual(model_a["backend_model_name"], "folder/a.safetensors")
        self.assertEqual(model_a["is_image_generation"], 1)
        self.assertEqual(model_a["is_rolled_out"], 0)

        db.update_model(
            model_a["id"], display_name="Admin Name", description="Admin description",
            is_rolled_out=1, sort_order=9,
        )
        with mock.patch.object(
            comfyui, "_get_json", return_value=_object_info(["folder/a.safetensors"])
        ):
            comfyui.sync_models()
        model_a = db.get_model_by_id(model_a["id"])
        model_b = db.get_model_by_id(model_b["id"])
        self.assertEqual(model_a["display_name"], "Admin Name")
        self.assertEqual(model_a["description"], "Admin description")
        self.assertEqual(model_a["is_rolled_out"], 1)
        self.assertEqual(model_a["sort_order"], 9)
        self.assertEqual(model_a["backend_available"], 1)
        self.assertEqual(model_b["backend_available"], 0)

    def test_failed_discovery_does_not_mark_catalog_models_absent(self):
        db.sync_comfyui_models(["still-there.safetensors"])
        with mock.patch.object(
            comfyui, "discover_checkpoints", side_effect=comfyui.ComfyUIError("offline")
        ):
            with self.assertRaises(comfyui.ComfyUIError):
                comfyui.sync_models()
        model = db.get_model_by_ollama_name("comfyui:still-there.safetensors")
        self.assertEqual(model["backend_available"], 1)

    def test_discovery_verifies_every_core_node(self):
        loader_only = {
            "CheckpointLoaderSimple": {
                "input": {"required": {"ckpt_name": [["a.safetensors"]]}}
            }
        }

        def get_json(path, timeout=None):
            if path == "/object_info/CheckpointLoaderSimple":
                return loader_only
            return {path.rsplit("/", 1)[-1]: {}}

        with mock.patch.object(comfyui, "_get_json", side_effect=get_json) as get:
            self.assertEqual(comfyui.discover_checkpoints(), ["a.safetensors"])
        paths = [call.args[0] for call in get.call_args_list]
        for node in comfyui._CORE_NODES:
            self.assertIn(f"/object_info/{node}", paths)

    def test_workflow_is_fixed_to_standard_core_nodes(self):
        workflow = comfyui.build_workflow(
            "sdxl.safetensors", "a yellow banana", 1024, 768, seed=42
        )
        self.assertEqual(
            {node["class_type"] for node in workflow.values()},
            set(comfyui._CORE_NODES),
        )
        self.assertEqual(workflow["1"]["inputs"], {"ckpt_name": "sdxl.safetensors"})
        self.assertEqual(workflow["4"]["inputs"], {
            "width": 1024, "height": 768, "batch_size": 1,
        })
        self.assertEqual(workflow["5"]["inputs"]["seed"], 42)
        self.assertEqual(workflow["5"]["inputs"]["positive"], ["2", 0])
        self.assertEqual(workflow["7"]["inputs"], {"images": ["6", 0]})
        self.assertNotIn("workflow", str(workflow).lower())

    def test_generation_polls_history_fetches_view_and_deletes_own_history(self):
        history = [
            {},
            {
                "prompt-1": {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {
                        "7": {"images": [{
                            "filename": "preview.png", "subfolder": "", "type": "temp",
                        }]}
                    },
                }
            },
        ]

        def post(path, body, timeout=None):
            if path == "/prompt":
                self.assertEqual(body["prompt"]["1"]["inputs"]["ckpt_name"], "sdxl.safetensors")
                return {"prompt_id": "prompt-1", "node_errors": {}}
            self.assertEqual(path, "/history")
            self.assertEqual(body, {"delete": ["prompt-1"]})
            return {}

        with mock.patch.object(comfyui, "_post_json", side_effect=post) as posted, \
             mock.patch.object(comfyui, "_get_json", side_effect=history), \
             mock.patch.object(comfyui, "_get_bytes", return_value=b"\x89PNG\r\n\x1a\nimage") as view, \
             mock.patch.object(comfyui.time, "sleep"):
            image = comfyui.generate_image("sdxl.safetensors", "banana", 512, 512)
        self.assertTrue(image.startswith(b"\x89PNG"))
        view.assert_called_once_with(
            "/view?filename=preview.png&subfolder=&type=temp",
            timeout=config.COMFYUI_TIMEOUT,
        )
        self.assertEqual(posted.call_count, 2)

    def test_malicious_or_unexpected_output_descriptors_are_rejected(self):
        invalid = [
            {"filename": "../secret.png", "subfolder": "", "type": "temp"},
            {"filename": "ok.png", "subfolder": "../../output", "type": "temp"},
            {"filename": "ok.png", "subfolder": "", "type": "output"},
        ]
        for descriptor in invalid:
            history = {"p": {"outputs": {"7": {"images": [descriptor]}}}}
            with self.subTest(descriptor=descriptor):
                with self.assertRaises(comfyui.ComfyUIError):
                    comfyui._history_output(history, "p")

        with self.assertRaisesRegex(comfyui.ComfyUIError, "output node"):
            comfyui._history_output(
                {"p": {"outputs": {"99": {"images": []}}}}, "p"
            )

    def test_generation_errors_and_timeout_are_controlled_and_cleaned_up(self):
        with mock.patch.object(
            comfyui, "_post_json", return_value={"prompt_id": "p", "node_errors": {}}
        ) as post, mock.patch.object(
            comfyui, "_get_json", return_value={"p": {"status": {"status_str": "failed"}}}
        ):
            with self.assertRaisesRegex(comfyui.ComfyUIError, "failed"):
                comfyui.generate_image("a.ckpt", "banana", 512, 512)
        self.assertEqual(post.call_args_list[-1].args[0], "/history")

        with mock.patch.object(config, "COMFYUI_GENERATION_TIMEOUT", 1), \
             mock.patch.object(comfyui, "_post_json", return_value={"prompt_id": "p", "node_errors": {}}), \
             mock.patch.object(comfyui.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaises(comfyui.ComfyUITimeoutError):
                comfyui.generate_image("a.ckpt", "banana", 512, 512)

    def test_failed_generation_uses_targeted_cancel_then_queue_fallback(self):
        calls = []

        def post(path, body, timeout=None):
            calls.append((path, body))
            if path == "/prompt":
                return {"prompt_id": "prompt-1", "node_errors": {}}
            if path == "/api/jobs/prompt-1/cancel":
                raise comfyui.ComfyUIError("endpoint unavailable")
            return {}

        with mock.patch.object(comfyui, "_post_json", side_effect=post), \
             mock.patch.object(
                 comfyui, "_get_json",
                 return_value={"prompt-1": {"status": {"status_str": "failed"}}},
             ):
            with self.assertRaises(comfyui.ComfyUIError):
                comfyui.generate_image("a.ckpt", "banana", 512, 512)
        self.assertEqual(
            calls[1:],
            [
                ("/api/jobs/prompt-1/cancel", {}),
                ("/queue", {"delete": ["prompt-1"]}),
                ("/history", {"delete": ["prompt-1"]}),
            ],
        )
        self.assertFalse(any(path == "/interrupt" for path, _body in calls))

    def test_http_errors_are_wrapped(self):
        error = urllib.error.HTTPError("http://comfy", 500, "bad", {}, None)
        with mock.patch.object(comfyui._opener, "open", side_effect=error):
            with self.assertRaisesRegex(comfyui.ComfyUIError, "HTTP 500"):
                comfyui._get_json("/object_info/CheckpointLoaderSimple")

    def test_comfyui_redirects_are_rejected(self):
        request = comfyui.urllib.request.Request("http://127.0.0.1:8188/prompt")
        self.assertIsNone(
            comfyui._NoRedirectHandler().redirect_request(
                request, None, 302, "Found", {}, "http://attacker.invalid/"
            )
        )
        redirect = urllib.error.HTTPError(
            request.full_url, 302, "Found", {"Location": "http://attacker.invalid/"}, None
        )
        with mock.patch.object(comfyui._opener, "open", side_effect=redirect):
            with self.assertRaisesRegex(comfyui.ComfyUIError, "HTTP 302"):
                comfyui._get_json("/object_info/CheckpointLoaderSimple")

    def test_image_resolution_never_syncs_or_exposes_backend_errors(self):
        with mock.patch.object(
            comfyui, "sync_models", side_effect=RuntimeError("secret backend detail")
        ) as sync:
            with self.assertRaisesRegex(
                image_generation.ImageGenerationError, "Image model not found"
            ) as raised:
                image_generation.resolve_model(self.user, "missing")
        sync.assert_not_called()
        self.assertNotIn("secret", str(raised.exception))

    def test_backend_isolation_for_text_and_image_paths(self):
        with db.get_db_context() as conn:
            conn.execute(
                "INSERT INTO ai_models "
                "(ollama_name, backend, backend_model_name, backend_available, display_name, "
                "is_rolled_out, is_image_generation) VALUES ('text', 'ollama', 'text', 1, 'Text', 1, 0)"
            )
            conn.execute(
                "INSERT INTO ai_models "
                "(ollama_name, backend, backend_model_name, backend_available, display_name, "
                "is_rolled_out, is_image_generation) VALUES "
                "('ollama-image', 'ollama', 'ollama-image', 1, 'Wrong Image', 1, 1)"
            )
            conn.commit()
        comfy = db.sync_comfyui_models(["image.safetensors"])[0]
        db.set_model_rollout(comfy["id"], True)

        text_models = model_access.list_accessible_models(self.user, "api")
        image_models = model_access.list_accessible_models(self.user, "api", image_only=True)
        self.assertEqual([model["ollama_name"] for model in text_models], ["text"])
        self.assertEqual(
            [model["ollama_name"] for model in image_models],
            ["comfyui:image.safetensors"],
        )
        with self.assertRaisesRegex(
            image_generation.ImageGenerationError, "does not support image generation"
        ):
            image_generation.resolve_model(self.admin, "ollama-image")
        with mock.patch.object(
            ollama, "list_available_models", return_value=[{"name": "text"}]
        ), mock.patch.object(ollama, "list_running_models", return_value=[]):
            selected, error = ollama.select_auto_model(self.user, "api")
        self.assertIsNone(error)
        self.assertEqual(selected["ollama_name"], "text")

    def test_legacy_migration_backfills_ollama_backend_identity(self):
        legacy_path = os.path.join(self.directory.name, "legacy.db")
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
            "INSERT INTO ai_models(ollama_name, display_name) VALUES ('legacy', 'Legacy')"
        )
        conn.commit()
        conn.close()

        config.DATABASE_PATH = legacy_path
        db.init_db()
        model = db.get_model_by_ollama_name("legacy")
        self.assertEqual(model["backend"], "ollama")
        self.assertEqual(model["backend_model_name"], "legacy")
        self.assertEqual(model["backend_available"], 1)
        self.assertIsNotNone(model["backend_last_seen_at"])


if __name__ == "__main__":
    unittest.main()
