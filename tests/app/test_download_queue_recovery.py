"""Downloads never wait in the queue for ever: per-backend slots, the orphan watchdog, visible waiting reasons."""

from __future__ import annotations

import threading
import time

from bananachat import db
from bananachat.db import pulls as pulls_db
from bananachat.services import background, health, pulls


def _checkpoint_job(name="slow.safetensors"):
    return pulls_db.enqueue(name, None, backend="comfyui", repo_id="owner/repo", source_filename=name,
                            revision="main", target_name=name, expected_sha256="a" * 64, expected_size=10)


def test_a_checkpoint_waiting_in_the_agent_queue_does_not_block_model_downloads(app):
    with app.app_context():
        checkpoint = _checkpoint_job()
        model = pulls.enqueue_ollama("tiny:1b", None)
        first = pulls_db.claim_next(1)
        assert first["id"] == checkpoint and first["status"] == "pulling"
        # Before: the single slot was taken by the checkpoint and the model stayed "queued".
        second = pulls_db.claim_next(1)
        assert second is not None and second["id"] == model
        assert pulls_db.claim_next(1) is None  # both backends are at their limit now


def test_a_skipped_backend_keeps_its_place_and_says_why(make_app):
    remote = make_app(INFERENCE_LOCAL="0")
    with remote.app_context():
        job = pulls.enqueue_ollama("tiny:1b", None)
        health.record_probe(False, 1, "unreachable")
        try:
            assert pulls_db.claim_next(1, skip_backend="ollama") is None
            assert pulls_db.note_waiting("ollama", pulls.WAITING_FOR_SERVER) == 1
            assert pulls_db.note_waiting("ollama", pulls.WAITING_FOR_SERVER) == 0  # written once
            row = pulls_db.get(job)
            assert row["status"] == "queued" and row["progress_detail"] == pulls.WAITING_FOR_SERVER
        finally:
            health.reset()


def test_a_stale_outage_record_is_not_believed(app):
    from bananachat.db import settings as site_settings

    with app.app_context():
        health.record_probe(False, 1, "unreachable")
        assert health.inference_down(max_age=0)
        state = site_settings.state_get(health.STATE_KEY)
        state["checked_at"] = time.time() - health.STALE_SECONDS - 5
        site_settings.state_set(health.STATE_KEY, state)
        assert not health.inference_down(max_age=0)
        health.reset()


def test_the_watchdog_requeues_a_job_whose_run_vanished(app):
    with app.app_context():
        job = pulls.enqueue_ollama("tiny:1b", None)
        claimed = pulls_db.claim_next(1)
        assert claimed["id"] == job
        # The thread that claimed it died without writing an outcome, long ago.
        db.execute("UPDATE model_pull_jobs SET progress_at='2000-01-01 00:00:00' WHERE id=?", (job,))
        assert pulls.recover_orphans(5) == [job]
        row = pulls_db.get(job)
        assert row["status"] == "queued" and row["claim_token"] is None
        assert "queued again" in row["progress_detail"]
        # A recent one is left alone, and so is one this process is running.
        claimed = pulls_db.claim_next(1)
        assert pulls.recover_orphans(5) == []
        db.execute("UPDATE model_pull_jobs SET progress_at='2000-01-01 00:00:00' WHERE id=?", (job,))
        with pulls._lock:
            pulls._running[job] = object()
        try:
            assert pulls.recover_orphans(5) == []
        finally:
            with pulls._lock:
                pulls._running.pop(job, None)
        assert pulls_db.get(job)["claim_token"] == claimed["claim_token"]


def test_retry_now_skips_the_back_off(app):
    with app.app_context():
        job = pulls.enqueue_ollama("tiny:1b", None)
        claimed = pulls_db.claim_next(1)
        pulls._retry_or_fail(claimed, pulls.DownloadProblem("connection reset", transient=True))
        assert pulls_db.get(job)["next_attempt_at"]
        assert pulls_db.claim_next(1) is None
        assert pulls.retry_now(job) is True
        assert pulls.retry_now(job) is False
        assert pulls_db.claim_next(1)["id"] == job


def test_retry_now_from_the_downloads_page(app, admin):
    with app.app_context():
        job = pulls.enqueue_ollama("tiny:1b", None)
        claimed = pulls_db.claim_next(1)
        pulls._retry_or_fail(claimed, pulls.DownloadProblem("connection reset", transient=True))
    page = admin.get("/admin/models/downloads")
    assert b"Retry now" in page.data
    response = admin.post(f"/admin/models/downloads/{job}/retry-now")
    assert response.status_code in (302, 303)
    with app.app_context():
        assert pulls_db.get(job)["next_attempt_at"] is None


def test_the_downloads_page_warns_when_no_process_runs_background_services(app, admin):
    with app.app_context():
        pulls.enqueue_ollama("tiny:1b", None)
    assert b"No server process is running the background services" in admin.get("/admin/models/downloads").data
    with app.app_context():
        background._beat(app)
    assert b"No server process is running the background services" not in admin.get("/admin/models/downloads").data


def test_one_hanging_periodic_job_does_not_hold_up_the_others(app, monkeypatch):
    release = threading.Event()
    ticks = []
    monkeypatch.setattr(background, "_jobs", {
        "hangs": background.Job("hangs", 1, lambda _app: release.wait(10), initial_delay=0),
        "ticks": background.Job("ticks", 1, lambda _app: ticks.append(time.monotonic()), initial_delay=0),
        "broken-interval": background.Job("broken-interval", lambda _app: 1 / 0, lambda _app: None, initial_delay=0),
    })
    background.start(app)
    try:
        deadline = time.monotonic() + 6
        while len(ticks) < 2 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert len(ticks) >= 2
        assert background.leader_status()["alive"] is True
    finally:
        release.set()
        background.stop(timeout=10)
        background._stop.clear()
