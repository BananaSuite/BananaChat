"""Download completion and cancellation races, without downloading model weights."""

from __future__ import annotations

import threading

import pytest

from bananachat import db
from bananachat.db import catalog
from bananachat.db import pulls as pulls_db
from bananachat.services import checkpoint_agent, ollama, pulls

WAIT_SECONDS = 5
REMOTE_ID = "a" * 32
CHECKPOINT = "race.safetensors"
MODEL = "race:1b"


class Gate:
    """Hold a backend response while another process changes the local job."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait(self):
        self.entered.set()
        assert self.release.wait(WAIT_SECONDS), "Backend gate was not released."


class Agent:
    def __init__(self, gate):
        self.gate = gate
        self.cancelled = []

    def submit_download(self, **recipe):
        return {"id": REMOTE_ID}

    def get_download(self, remote_id):
        assert remote_id == REMOTE_ID
        self.gate.wait()
        return {"id": remote_id, "status": "completed", "bytes_received": 100, "expected_size": 100}

    def cancel_download(self, remote_id):
        self.cancelled.append(remote_id)


def start_download(app):
    errors = []

    def run():
        with app.app_context():
            try:
                assert pulls.process_next(stopping=lambda: False)
            except BaseException as error:
                errors.append(error)
            finally:
                db.release_thread_connection()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, errors


def finish_download(worker, errors, gate):
    gate.release.set()
    worker.join(WAIT_SECONDS)
    assert not worker.is_alive(), "Download worker did not finish."
    assert not errors, errors


def enqueue_checkpoint(app):
    with app.app_context():
        return pulls_db.enqueue(CHECKPOINT, None, backend="comfyui", repo_id="owner/repo",
                                source_filename=CHECKPOINT, revision="main", target_name=CHECKPOINT,
                                expected_sha256="a" * 64, expected_size=100)


@pytest.mark.parametrize("cleanup", [True, False])
def test_cancel_during_checkpoint_status_response_does_not_publish(make_app, monkeypatch, cleanup):
    app = make_app(image_backend="comfyui")
    gate = Gate()
    agent = Agent(gate)
    synced = []
    monkeypatch.setattr(checkpoint_agent, "client", lambda config=None: agent)
    monkeypatch.setattr(pulls, "_sync_comfyui", lambda: synced.append(CHECKPOINT))
    job_id = enqueue_checkpoint(app)
    worker, errors = start_download(app)
    try:
        assert gate.entered.wait(WAIT_SECONDS)
        with app.app_context():
            assert pulls.cancel(job_id, cleanup=cleanup)
    finally:
        finish_download(worker, errors, gate)
    with app.app_context():
        assert pulls_db.get(job_id)["status"] == "cancelled"
    assert synced == []
    assert agent.cancelled == ([REMOTE_ID] if cleanup else [])


def test_cancel_during_ollama_verification_cleans_new_download(app, monkeypatch):
    gate = Gate()
    deleted, synced = [], []
    monkeypatch.setattr(ollama, "installed_names", lambda *args, **kwargs: set())
    monkeypatch.setattr(ollama, "pull", lambda *args, **kwargs: iter([{"status": "success"}]))

    def listing(*args, **kwargs):
        gate.wait()
        return [{"name": MODEL, "digest": "verified-digest"}]

    monkeypatch.setattr(ollama, "list_tags", listing)
    monkeypatch.setattr(ollama, "delete", lambda name, config=None: deleted.append(name))
    monkeypatch.setattr(ollama, "sync_catalog", lambda *args, **kwargs: synced.append(MODEL))
    with app.app_context():
        job_id = pulls.enqueue_ollama(MODEL, None)
    worker, errors = start_download(app)
    try:
        assert gate.entered.wait(WAIT_SECONDS)
        with app.app_context():
            assert pulls.cancel(job_id)
    finally:
        finish_download(worker, errors, gate)
    with app.app_context():
        assert pulls_db.get(job_id)["status"] == "cancelled"
    assert deleted == [MODEL]
    assert synced == []


def test_stale_checkpoint_completion_does_not_publish_or_cancel_new_claim(make_app, monkeypatch):
    app = make_app(image_backend="comfyui")
    gate = Gate()
    agent = Agent(gate)
    synced = []
    monkeypatch.setattr(checkpoint_agent, "client", lambda config=None: agent)
    monkeypatch.setattr(pulls, "_sync_comfyui", lambda: synced.append(CHECKPOINT))
    job_id = enqueue_checkpoint(app)
    worker, errors = start_download(app)
    try:
        assert gate.entered.wait(WAIT_SECONDS)
        with app.app_context():
            old = pulls_db.get(job_id)
            assert pulls_db.reset_stuck() == 1
            new = pulls_db.claim_next(1)
            assert new["claim_token"] != old["claim_token"]
    finally:
        finish_download(worker, errors, gate)
    with app.app_context():
        current = pulls_db.get(job_id)
        assert (current["status"], current["claim_token"]) == ("pulling", new["claim_token"])
    assert synced == []
    assert agent.cancelled == []


def test_old_cancel_cleanup_does_not_delete_replacement_pull(app, monkeypatch):
    gate = Gate()
    deleted = []
    monkeypatch.setattr(ollama, "installed_names", lambda *args, **kwargs: set())

    def download(*args, **kwargs):
        gate.wait()
        yield {"status": "pulling", "completed": 1, "total": 100}

    monkeypatch.setattr(ollama, "pull", download)
    monkeypatch.setattr(ollama, "delete", lambda name, config=None: deleted.append(name))
    with app.app_context():
        old_id = pulls.enqueue_ollama(MODEL, None)
    worker, errors = start_download(app)
    try:
        assert gate.entered.wait(WAIT_SECONDS)
        with app.app_context():
            assert pulls.cancel(old_id)
            replacement_id = pulls.enqueue_ollama(MODEL, None)
            replacement = pulls_db.claim_next(1)
            assert replacement["id"] == replacement_id
    finally:
        finish_download(worker, errors, gate)
    with app.app_context():
        assert pulls_db.get(old_id)["status"] == "cancelled"
        current = pulls_db.get(replacement_id)
        assert (current["status"], current["claim_token"]) == ("pulling", replacement["claim_token"])
    assert deleted == []


def test_stale_ollama_verification_does_not_publish_or_delete_new_claim(app, monkeypatch):
    gate = Gate()
    deleted, synced = [], []
    monkeypatch.setattr(ollama, "installed_names", lambda *args, **kwargs: set())
    monkeypatch.setattr(ollama, "pull", lambda *args, **kwargs: iter([{"status": "success"}]))

    def listing(*args, **kwargs):
        gate.wait()
        return [{"name": MODEL, "digest": "verified-digest"}]

    monkeypatch.setattr(ollama, "list_tags", listing)
    monkeypatch.setattr(ollama, "delete", lambda name, config=None: deleted.append(name))
    monkeypatch.setattr(ollama, "sync_catalog", lambda *args, **kwargs: synced.append(MODEL))
    with app.app_context():
        job_id = pulls.enqueue_ollama(MODEL, None)
    worker, errors = start_download(app)
    try:
        assert gate.entered.wait(WAIT_SECONDS)
        with app.app_context():
            old = pulls_db.get(job_id)
            assert pulls_db.reset_stuck() == 1
            new = pulls_db.claim_next(1)
            assert new["claim_token"] != old["claim_token"]
    finally:
        finish_download(worker, errors, gate)
    with app.app_context():
        current = pulls_db.get(job_id)
        assert (current["status"], current["claim_token"]) == ("pulling", new["claim_token"])
    assert deleted == []
    assert synced == []


def test_deleting_verified_retry_history_does_not_revive_old_cancellation(app, fake_ollama, monkeypatch):
    """A delayed catalog sync can still discover a verified retry after its history is cleared."""
    from bananachat.services.upstream import UpstreamError

    def unavailable_sync(*args, **kwargs):
        raise UpstreamError("Catalog sync temporarily unavailable.")

    with app.app_context():
        old_id = pulls.enqueue_ollama(MODEL, None)
        assert pulls.cancel(old_id, cleanup=False)
        retry_id = pulls.enqueue_ollama(MODEL, None)
        with monkeypatch.context() as unavailable:
            unavailable.setattr(ollama, "sync_catalog", unavailable_sync)
            assert pulls.process_next(stopping=lambda: False)
        retry = pulls_db.get(retry_id)
        assert retry["status"] == "done" and retry["digest"] == fake_ollama.digest(MODEL)
        assert catalog.get_by_name(MODEL) is None
        assert pulls_db.delete_finished(retry_id)
        assert pulls_db.clear_finished() == 1
        ollama.sync_catalog(source="test")
        discovered = catalog.get_by_name(MODEL)
        assert discovered is not None and discovered["enrollment"] == "new"
