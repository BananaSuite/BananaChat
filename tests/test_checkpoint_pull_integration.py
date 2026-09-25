"""Focused integration tests for web-side checkpoint pull handling."""

import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest import mock


_module_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("BC_ENV", "testing")
os.environ.setdefault("BC_INSTANCE_DIR", _module_tmp.name)
os.environ.setdefault("BC_DATABASE_PATH", os.path.join(_module_tmp.name, "tests.db"))
os.environ.setdefault("BC_LOGGING_LEVEL", "off")

import config  # noqa: E402
import db  # noqa: E402
from app import app  # noqa: E402
from services import checkpoint_agent, comfyui, ollama  # noqa: E402


SHA256 = "a" * 64
REMOTE_ID = "b" * 32


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class _Opener:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {"status": "ok"}
        self.error = error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        return _Response(json.dumps(self.payload).encode("utf-8"))


class _PullStop:
    def is_set(self):
        return False

    def wait(self, timeout):
        return False


class CheckpointPullIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_database = config.DATABASE_PATH
        self.previous_backend = config.IMAGE_BACKEND
        self.previous_agent_url = config.CHECKPOINT_AGENT_URL
        self.previous_agent_token = config.CHECKPOINT_AGENT_TOKEN_FILE
        self.previous_insecure_tailscale = config.CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE
        config.DATABASE_PATH = os.path.join(self.directory.name, "pulls.db")
        config.IMAGE_BACKEND = "comfyui"
        config.CHECKPOINT_AGENT_URL = ""
        config.CHECKPOINT_AGENT_TOKEN_FILE = ""
        config.CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE = False
        db.init_db()
        with db.get_db_context() as conn:
            conn.execute(
                "INSERT INTO users(id, username, password, role) VALUES (?,?,?,?)",
                ("admin-1", "admin-one", "unused", "admin"),
            )
            conn.execute("UPDATE site_settings SET setup_done=1 WHERE id=1")
            conn.commit()
        ollama._pull_stop.clear()

    def tearDown(self):
        config.DATABASE_PATH = self.previous_database
        config.IMAGE_BACKEND = self.previous_backend
        config.CHECKPOINT_AGENT_URL = self.previous_agent_url
        config.CHECKPOINT_AGENT_TOKEN_FILE = self.previous_agent_token
        config.CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE = self.previous_insecure_tailscale
        ollama._pull_stop.clear()
        self.directory.cleanup()

    def _enqueue_checkpoint(self, target="models/test.safetensors", size=100):
        return db.enqueue_pull_job(
            target,
            "admin-1",
            backend="comfyui",
            repo_id="owner/repo",
            source_filename="source/test.safetensors",
            revision="0123456789abcdef",
            target_name=target,
            expected_sha256=SHA256,
            expected_size=size,
        )

    def _write_token(self, value="agent-secret"):
        path = os.path.join(self.directory.name, "agent.token")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, value.encode("utf-8"))
        finally:
            os.close(fd)
        return path

    def test_legacy_pull_migration_defaults_backend_to_ollama(self):
        legacy_path = os.path.join(self.directory.name, "legacy.db")
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            "CREATE TABLE model_pull_jobs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ollama_name TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'queued', progress_pct INTEGER NOT NULL DEFAULT 0, "
            "progress_detail TEXT NOT NULL DEFAULT '', error_message TEXT, requested_by TEXT, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')), started_at TEXT, finished_at TEXT)"
        )
        conn.execute("INSERT INTO model_pull_jobs(ollama_name) VALUES ('legacy:latest')")
        conn.commit()
        conn.close()

        config.DATABASE_PATH = legacy_path
        db.init_db()
        job = db.list_pull_jobs()[0]
        self.assertEqual(job["backend"], "ollama")
        self.assertIsNone(job["repo_id"])
        self.assertIsNone(job["remote_job_id"])
        self.assertRegex(job["idempotency_key"], r"^[0-9a-f]{32}$")

    def test_enqueue_validates_metadata_and_prevents_active_backend_target_duplicate(self):
        job_id = self._enqueue_checkpoint()
        job = db.get_pull_job(job_id)
        self.assertEqual(job["backend"], "comfyui")
        self.assertEqual(job["ollama_name"], "models/test.safetensors")
        self.assertEqual(job["expected_sha256"], SHA256)
        with self.assertRaisesRegex(ValueError, "already queued"):
            self._enqueue_checkpoint()
        with self.assertRaises(ValueError):
            db.enqueue_pull_job(
                "bad.safetensors", "admin-1", backend="comfyui",
                repo_id="https://example.invalid/signed?token=secret",
                source_filename="bad.safetensors", revision="main",
                target_name="bad.safetensors", expected_sha256=SHA256,
            )
        with self.assertRaisesRegex(ValueError, "backend"):
            db.enqueue_pull_job("model", "admin-1", backend="other")

    def test_client_sends_bearer_header_without_leaking_token_in_errors(self):
        token = "top-secret-agent-token"
        token_path = self._write_token(token)
        client = checkpoint_agent.CheckpointAgentClient(
            "http://127.0.0.1:8765", token_path
        )
        opener = _Opener()
        client._opener = opener
        self.assertEqual(client.status(), {"status": "ok"})
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer " + token)
        self.assertNotIn(token, repr(client))

        error_body = json.dumps({
            "error": {
                "message": "failed at https://example.invalid/file?sig=secret Bearer stolen"
            }
        }).encode("utf-8")
        http_error = urllib.error.HTTPError(
            "http://127.0.0.1:8765/v1/status", 500, "error", {}, io.BytesIO(error_body)
        )
        client._opener = _Opener(error=http_error)
        with self.assertRaises(checkpoint_agent.CheckpointAgentError) as raised:
            client.status()
        message = str(raised.exception)
        self.assertNotIn("sig=secret", message)
        self.assertNotIn("stolen", message)
        self.assertNotIn(token, message)

        os.chmod(token_path, 0o644)
        with self.assertRaisesRegex(ValueError, "mode-0600"):
            checkpoint_agent.CheckpointAgentClient(
                "http://127.0.0.1:8765", token_path
            )
        with self.assertRaisesRegex(ValueError, "configured correctly"):
            checkpoint_agent.CheckpointAgentClient(
                "http://127.0.0.1:8765/path?token=secret", token_path
            )

    def test_checkpoint_agent_requires_https_off_loopback(self):
        token_path = self._write_token()
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            checkpoint_agent.CheckpointAgentClient(
                "http://192.0.2.10:8765", token_path
            )
        client = checkpoint_agent.CheckpointAgentClient(
            "https://agent.example.test", token_path
        )
        self.assertEqual(client.base_url, "https://agent.example.test")
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            checkpoint_agent.CheckpointAgentClient(
                "http://100.64.1.2:8765", token_path
            )
        config.CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE = True
        with self.assertLogs("bananachat.checkpoint_agent", level="WARNING") as logs:
            tailscale = checkpoint_agent.CheckpointAgentClient(
                "http://node.tailnet.ts.net:8765", token_path
            )
        self.assertEqual(tailscale.base_url, "http://node.tailnet.ts.net:8765")
        self.assertIn("INSECURE CHECKPOINT AGENT TRANSPORT", " ".join(logs.output))

    def test_submit_poll_completion_persists_remote_id_and_syncs_before_done(self):
        job_id = self._enqueue_checkpoint()
        job = db.claim_next_pull_job()
        events = []

        class Client:
            def submit_download(self, **kwargs):
                events.append(("submit", kwargs))
                return {"id": REMOTE_ID, "status": "queued"}

            def get_download(self, remote_id):
                events.append(("poll", remote_id))
                return {
                    "id": remote_id, "status": "completed",
                    "bytes_received": 100, "expected_size": 100,
                }

            def cancel_download(self, remote_id):
                events.append(("cancel", remote_id))

        def sync():
            self.assertEqual(db.get_pull_job(job_id)["status"], "pulling")
            events.append(("sync", None))
            return [{
                "backend_model_name": "models/test.safetensors",
                "backend_available": 1,
            }]

        with mock.patch.object(checkpoint_agent, "get_client", return_value=Client()), \
             mock.patch.object(comfyui, "sync_models", side_effect=sync), \
             mock.patch.object(ollama, "_pull_stop", _PullStop()):
            ollama._process_single_pull(job)

        result = db.get_pull_job(job_id)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["remote_job_id"], REMOTE_ID)
        self.assertEqual(result["progress_pct"], 100)
        self.assertEqual(
            events[0][1]["idempotency_key"],
            db.get_pull_job(job_id)["idempotency_key"],
        )
        self.assertEqual([event[0] for event in events], ["submit", "poll", "sync"])

    def test_completed_agent_job_fails_if_comfyui_does_not_see_target(self):
        job_id = self._enqueue_checkpoint("missing.safetensors")
        job = db.claim_next_pull_job()

        class Client:
            def submit_download(self, **kwargs):
                return {"id": REMOTE_ID}

            def get_download(self, remote_id):
                return {
                    "id": remote_id,
                    "status": "completed",
                    "bytes_received": 100,
                    "expected_size": 100,
                }

        with mock.patch.object(checkpoint_agent, "get_client", return_value=Client()), \
             mock.patch.object(comfyui, "sync_models", return_value=[]), \
             mock.patch.object(ollama, "_pull_stop", _PullStop()):
            ollama._process_single_pull(job)
        result = db.get_pull_job(job_id)
        self.assertEqual(result["status"], "failed")
        self.assertIn("did not report the target", result["error_message"])

    def test_restart_requeues_comfyui_remote_job_but_fails_ollama_pull(self):
        comfy_id = self._enqueue_checkpoint("resume.safetensors")
        comfy_job = db.claim_next_pull_job()
        db.set_pull_job_remote_id(comfy_id, REMOTE_ID)
        with db.get_db_context() as conn:
            conn.execute(
                "INSERT INTO model_pull_jobs(ollama_name, backend, status) "
                "VALUES ('text:latest', 'ollama', 'pulling')"
            )
            ollama_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
        db.reset_stuck_pulls()
        resumed = db.get_pull_job(comfy_id)
        failed = db.get_pull_job(ollama_id)
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["remote_job_id"], REMOTE_ID)
        self.assertEqual(failed["status"], "failed")

        events = []

        class Client:
            def submit_download(self, **kwargs):
                raise AssertionError("reattachment must not submit a second remote job")

            def get_download(self, remote_id):
                events.append(remote_id)
                return {
                    "id": remote_id,
                    "status": "completed",
                    "bytes_received": 100,
                    "expected_size": 100,
                }

        claimed = db.claim_next_pull_job()
        self.assertEqual(claimed["id"], comfy_job["id"])
        with mock.patch.object(checkpoint_agent, "get_client", return_value=Client()), \
             mock.patch.object(
                 comfyui,
                 "sync_models",
                 return_value=[{
                     "backend_model_name": "resume.safetensors",
                     "backend_available": 1,
                 }],
             ), mock.patch.object(ollama, "_pull_stop", _PullStop()):
            ollama._process_single_pull(claimed)
        self.assertEqual(events, [REMOTE_ID])
        self.assertEqual(db.get_pull_job(comfy_id)["status"], "done")

    def test_image_page_uses_catalog_without_comfyui_network_sync(self):
        previous_testing = app.config.get("TESTING")
        previous_csrf = app.config.get("WTF_CSRF_ENABLED")
        try:
            app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
            client = app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = "admin-1"
            with mock.patch.object(
                comfyui, "sync_models", side_effect=RuntimeError("raw backend secret")
            ) as sync:
                response = client.get("/images")
        finally:
            app.config.update(TESTING=previous_testing, WTF_CSRF_ENABLED=previous_csrf)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"raw backend secret", response.data)
        sync.assert_not_called()

    def test_admin_cannot_mark_ollama_model_as_image_generation(self):
        db.upsert_model("text-only:latest", "Text only")
        model = db.get_model_by_ollama_name("text-only:latest")
        with db.get_db_context() as conn:
            conn.execute(
                "UPDATE ai_models SET is_image_generation=1 WHERE id=?", (model["id"],)
            )
            conn.commit()
        previous_testing = app.config.get("TESTING")
        previous_csrf = app.config.get("WTF_CSRF_ENABLED")
        try:
            app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
            client = app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = "admin-1"
            page = client.get(f"/admin/models/{model['id']}/edit")
            response = client.post(
                f"/admin/models/{model['id']}/edit",
                data={
                    "display_name": "Text only",
                    "is_image_generation": "1",
                },
            )
        finally:
            app.config.update(TESTING=previous_testing, WTF_CSRF_ENABLED=previous_csrf)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b'name="is_image_generation"', page.data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_model_by_id(model["id"])["is_image_generation"], 0)

    def test_agent_failure_and_database_cancellation_are_terminal(self):
        failed_id = self._enqueue_checkpoint("failed.safetensors")
        failed_job = db.claim_next_pull_job()

        class FailedClient:
            def submit_download(self, **kwargs):
                return {"id": REMOTE_ID}

            def get_download(self, remote_id):
                return {
                    "id": remote_id, "status": "failed", "bytes_received": 10,
                    "expected_size": 100,
                    "error": {"code": "hash_mismatch", "message": "digest mismatch"},
                }

        with mock.patch.object(checkpoint_agent, "get_client", return_value=FailedClient()), \
             mock.patch.object(ollama, "_pull_stop", _PullStop()):
            ollama._process_single_pull(failed_job)
        failed = db.get_pull_job(failed_id)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("digest mismatch", failed["error_message"])

        canceled_id = self._enqueue_checkpoint("canceled.safetensors")
        canceled_job = db.claim_next_pull_job()
        canceled_remote = []

        class CanceledClient:
            def submit_download(self, **kwargs):
                return {"id": REMOTE_ID}

            def get_download(self, remote_id):
                db.cancel_pull_job(canceled_id)
                return {
                    "id": remote_id, "status": "downloading",
                    "bytes_received": 10, "expected_size": 100,
                }

            def cancel_download(self, remote_id):
                canceled_remote.append(remote_id)

        with mock.patch.object(checkpoint_agent, "get_client", return_value=CanceledClient()), \
             mock.patch.object(ollama, "_pull_stop", _PullStop()), \
             mock.patch.object(ollama, "delete_ollama_model") as ollama_delete:
            ollama._process_single_pull(canceled_job)
        self.assertEqual(db.get_pull_job(canceled_id)["status"], "cancelled")
        self.assertEqual(canceled_remote, [REMOTE_ID])
        ollama_delete.assert_not_called()

    def test_legacy_ollama_enqueue_claim_and_processing_remain_compatible(self):
        job_id = db.enqueue_pull_job("legacy:latest", "admin-1")
        job = db.claim_next_pull_job()
        self.assertEqual(job["backend"], "ollama")
        self.assertIsNone(job["target_name"])
        with mock.patch.object(ollama, "pull_model", return_value=True) as pull, \
             mock.patch.object(ollama, "sync_models", return_value=[]):
            ollama._process_single_pull(job)
        pull.assert_called_once()
        self.assertEqual(db.get_pull_job(job_id)["status"], "done")

    def test_malformed_ollama_model_response_preserves_catalog_availability(self):
        db.sync_ollama_models([{
            "name": "safe-model",
            "display_name": "Safe Model",
            "description": "test",
        }])
        with mock.patch.object(ollama, "_get", return_value={}):
            with self.assertRaises(ollama.OllamaConnectionError):
                ollama.sync_models()
        model = db.get_model_by_ollama_name("safe-model")
        self.assertEqual(model["backend_available"], 1)

    def test_admin_checkpoint_route_rejects_signed_url_metadata(self):
        previous_testing = app.config.get("TESTING")
        previous_csrf = app.config.get("WTF_CSRF_ENABLED")
        try:
            app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
            client = app.test_client()
            with client.session_transaction() as session:
                session["user_id"] = "admin-1"
            with mock.patch.object(comfyui, "is_enabled", return_value=True), \
                 mock.patch.object(checkpoint_agent, "configuration_error", return_value=None):
                response = client.post(
                    "/admin/models/pull-checkpoint",
                    data={
                        "repo_id": "https://example.invalid/repo?token=secret",
                        "source_filename": "source.safetensors",
                        "revision": "main",
                        "target_name": "target.safetensors",
                        "expected_sha256": SHA256,
                    },
                    follow_redirects=True,
                )
        finally:
            app.config.update(
                TESTING=previous_testing, WTF_CSRF_ENABLED=previous_csrf
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"owner/repo", response.data)
        self.assertEqual(db.list_pull_jobs(), [])


if __name__ == "__main__":
    unittest.main()
