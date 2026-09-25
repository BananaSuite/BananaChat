"""Fast public-release smoke tests for the app shell and language layer."""

import os
import multiprocessing
import tempfile
import time
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor


_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("BC_ENV", "testing")
os.environ.setdefault("BC_INSTANCE_DIR", _tmp.name)
os.environ.setdefault("BC_DATABASE_PATH", os.path.join(_tmp.name, "test.db"))
os.environ.setdefault("BC_LOGGING_LEVEL", "off")
os.environ.setdefault("BC_OLLAMA_SYNC_INTERVAL", "3600")
os.environ.setdefault("BC_COMPUTE_SNAPSHOT_INTERVAL", "3600")

from app import app  # noqa: E402
import db  # noqa: E402
from services import ollama  # noqa: E402
from services import queue as inference_queue  # noqa: E402


def _hold_queue_slot(active, peak, lock, ready):
    slot = inference_queue.acquire(timeout=10)
    with slot:
        with lock:
            active.value += 1
            peak.value = max(peak.value, active.value)
        ready.put(True)
        time.sleep(0.15)
        with lock:
            active.value -= 1


def _watch_cross_process_stop(session_id, ready, result):
    token = db.register_active_stream(session_id)
    ready.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        if db.active_stream_should_stop(session_id, token):
            result.put(True)
            db.deregister_active_stream(session_id, token)
            return
        time.sleep(0.05)
    result.put(False)


class AppShellTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app.test_client()

    def test_italian_is_the_default(self):
        response = self.client.get("/setup")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<html lang="it">', response.data)
        self.assertIn("Configurazione iniziale".encode(), response.data)

    def test_language_can_switch_to_english(self):
        response = self.client.get("/language/en?next=/setup", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<html lang="en">', response.data)
        self.assertIn(b"First-run setup", response.data)

    def test_security_headers_are_present(self):
        response = self.client.get("/setup")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("camera=()", response.headers["Permissions-Policy"])

    def test_database_health_checks_integrity_and_foreign_keys(self):
        health = db.get_database_health()
        self.assertTrue(health["healthy"])
        self.assertEqual(health["quick_check"], "ok")
        self.assertEqual(health["foreign_key_violations"], 0)
        self.assertEqual(health["journal_mode"], "wal")

    def test_compute_snapshot_keeps_ram_separate_from_vram(self):
        db.record_compute_snapshot(
            gpu_name="Test GPU",
            gpu_memory_used_mb=2048,
            gpu_memory_total_mb=8192,
            gpu_utilization_percent=25,
            system_memory_used_mb=4096,
            system_memory_total_mb=16384,
            metrics_source="test",
            active_models=["model-a"],
        )
        latest = db.get_latest_snapshot()
        self.assertEqual(latest["gpu_memory_used_mb"], 2048)
        self.assertEqual(latest["system_memory_used_mb"], 4096)
        self.assertEqual(latest["active_models"], ["model-a"])

    def test_ollama_fallback_reports_allocation_not_total_vram(self):
        captured = {}
        running = [{"name": "model-a", "size_vram": 2 * 1024 ** 3}]
        with mock.patch.object(ollama, "list_running_models", return_value=running), \
             mock.patch.object(ollama, "_nvidia_metrics", return_value=None), \
             mock.patch.object(ollama.db, "record_compute_snapshot", side_effect=lambda **kw: captured.update(kw)):
            ollama.collect_compute_snapshot(queue_depth=3)
        self.assertEqual(captured["gpu_memory_used_mb"], 2048)
        self.assertIsNone(captured["gpu_memory_total_mb"])
        self.assertEqual(captured["metrics_source"], "ollama-allocation")
        self.assertEqual(captured["queue_depth"], 3)

    def test_nvidia_metrics_are_aggregated(self):
        output = "GPU A, 1024, 4096, 25\nGPU B, 2048, 8192, 50\n"
        completed = mock.Mock(stdout=output)
        with mock.patch.object(ollama.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch.object(ollama.subprocess, "run", return_value=completed):
            result = ollama._nvidia_metrics()
        self.assertEqual(result["name"], "GPU A + GPU B")
        self.assertEqual(result["used_mb"], 3072)
        self.assertEqual(result["total_mb"], 12288)
        self.assertAlmostEqual(result["utilization"], 41.666, places=2)

    def test_concurrent_metric_writes_are_serialized_by_sqlite(self):
        with db.get_db_context() as conn:
            before = conn.execute("SELECT COUNT(*) FROM compute_snapshots").fetchone()[0]

        def write_one(index):
            db.record_compute_snapshot(cpu_percent=float(index), queue_depth=index)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write_one, range(32)))
        with db.get_db_context() as conn:
            after = conn.execute("SELECT COUNT(*) FROM compute_snapshots").fetchone()[0]
        self.assertEqual(after - before, 32)

    def test_queue_limit_is_shared_across_processes(self):
        ctx = multiprocessing.get_context("fork")
        previous = inference_queue.MAX_CONCURRENT
        inference_queue.MAX_CONCURRENT = 2
        with db.get_db_context() as conn:
            conn.execute("DELETE FROM inference_queue")
            conn.commit()
        active = ctx.Value("i", 0)
        peak = ctx.Value("i", 0)
        lock = ctx.Lock()
        ready = ctx.Queue()
        processes = [
            ctx.Process(target=_hold_queue_slot, args=(active, peak, lock, ready))
            for _ in range(6)
        ]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=10))
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(peak.value, 2)
            self.assertEqual(inference_queue.get_stats()["total"], 0)
        finally:
            inference_queue.MAX_CONCURRENT = previous
            for process in processes:
                if process.is_alive():
                    process.terminate()

    def test_stop_signal_crosses_process_boundary(self):
        ctx = multiprocessing.get_context("fork")
        ready = ctx.Event()
        result = ctx.Queue()
        session_id = "cross-process-stop-test"
        process = ctx.Process(
            target=_watch_cross_process_stop, args=(session_id, ready, result)
        )
        process.start()
        try:
            self.assertTrue(ready.wait(timeout=5))
            self.assertTrue(db.request_stop_stream(session_id))
            self.assertTrue(result.get(timeout=5))
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.terminate()

    def test_no_history_working_copy_expires_but_audit_is_explicitly_purgeable(self):
        user_id = "retention-user"
        with db.get_db_context() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users(id, username, password) VALUES(?,?,?)",
                (user_id, "retention-user", "unused"),
            )
            conn.commit()
        session_id = db.create_session(user_id, is_incognito=True)
        db.add_message(
            session_id, "user", "transient", is_incognito=True, user_id=user_id
        )
        with db.get_db_context() as conn:
            conn.execute(
                "UPDATE chat_sessions SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (session_id,),
            )
            conn.commit()
        self.assertEqual(db.purge_expired_incognito_sessions(hours=1), 1)
        self.assertIsNone(db.get_session(session_id))
        with db.get_db_context() as conn:
            audit_count = conn.execute(
                "SELECT COUNT(*) FROM incognito_audit WHERE session_id=?", (session_id,)
            ).fetchone()[0]
        self.assertEqual(audit_count, 1)
        db.delete_incognito_session(session_id)
        with db.get_db_context() as conn:
            audit_count = conn.execute(
                "SELECT COUNT(*) FROM incognito_audit WHERE session_id=?", (session_id,)
            ).fetchone()[0]
        self.assertEqual(audit_count, 0)


if __name__ == "__main__":
    unittest.main()
