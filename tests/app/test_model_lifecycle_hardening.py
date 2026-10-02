"""Lifecycle state and safe publication around downloads, outages and enrollment."""

from __future__ import annotations

import pytest

from tests.app.test_model_lifecycle import model, publish, sync


def test_publish_shortcut_applies_the_size_preset(app, admin, fake_ollama):
    from bananachat.db import limits

    fake_ollama.models = ["large:70b"]
    fake_ollama.details["large:70b"] = {"parameter_size": "70B"}
    sync(app)
    row = model(app, "large:70b")
    assert admin.post(f"/admin/models/{row['id']}/rollout", {"enabled": "1"}).status_code == 302
    with app.app_context():
        current = model(app, "large:70b")
        assert current["enrollment"] == "reviewed" and current["is_rolled_out"]
        assert current["limit_preset"] == "heavy"
        assert limits.get_model_policy(current)["weight"] == 3


def test_toggling_a_reviewed_model_keeps_custom_limits(app, admin, fake_ollama):
    from bananachat.db import limits

    sync(app)
    row = model(app, "llama3.2:3b")
    assert admin.post(f"/admin/models/{row['id']}/rollout", {"enabled": "1"}).status_code == 302
    with app.app_context():
        policy = limits.get_model_policy(row)
        custom = limits.set_model_policy(row["id"], {**policy, "weight": 7.5, "window_tokens": 123_000}, None)
    admin.post(f"/admin/models/{row['id']}/rollout", {"enabled": "0"})
    admin.post(f"/admin/models/{row['id']}/rollout", {"enabled": "1"})
    with app.app_context():
        assert limits.get_model_policy(row) == custom


@pytest.mark.parametrize("automatic", [False, True])
def test_failed_preset_cannot_publish_or_partially_change_limits(app, admin, fake_ollama, monkeypatch, automatic):
    from bananachat import db
    from bananachat.db import limits as limits_db
    from bananachat.services import limits, model_lifecycle

    sync(app)
    row = model(app, "llama3.2:3b")
    with app.app_context():
        before = limits_db.get_model_policy(row)

    def broken(model_id, preset, **_kwargs):
        db.execute("INSERT INTO runtime_state (key,value,updated_at) VALUES ('partial-preset','true',1)")
        raise RuntimeError("Preset storage failed")

    monkeypatch.setattr(limits, "apply_model_preset", broken)
    if automatic:
        with app.app_context():
            assert model_lifecycle.enroll_pending({"enrollment": "automatic"}) == []
    else:
        response = admin.post(f"/admin/models/{row['id']}/rollout", {"enabled": "1"})
        assert response.status_code == 302
    with app.app_context():
        current = model(app, "llama3.2:3b")
        assert current["enrollment"] == "new" and not current["is_rolled_out"]
        assert db.scalar("SELECT value FROM runtime_state WHERE key='partial-preset'") is None
        assert limits_db.get_model_policy(row) == before


@pytest.mark.parametrize("state", ["retired_at", "delete_requested_at", "failing_at"])
def test_automatic_enrollment_does_not_publish_withdrawn_models(app, fake_ollama, state):
    from bananachat.db import catalog
    from bananachat.services import model_lifecycle

    sync(app)
    with app.app_context():
        for row in catalog.list_models():
            catalog.set_lifecycle(row["id"], **{state: "2020-01-01 00:00:00"})
        assert model_lifecycle.enroll_pending({"enrollment": "automatic"}) == []
        assert all(not row["is_rolled_out"] for row in catalog.list_models())


def test_failed_backend_delete_does_not_suppress_recovery(app, fake_ollama, monkeypatch):
    from bananachat.db import catalog
    from bananachat.services import model_lifecycle, model_recovery, ollama
    from bananachat.services.upstream import UpstreamError

    sync(app)
    publish(app, "llama3.2:3b")
    row = model(app, "llama3.2:3b")

    def fail(*_args, **_kwargs):
        raise UpstreamError("Server unreachable", kind="connect")

    monkeypatch.setattr(ollama, "delete", fail)
    with app.app_context():
        with pytest.raises(UpstreamError):
            model_lifecycle.delete_from_server(row)
        assert catalog.get(row["id"])["delete_requested_at"] is None
        assert model_recovery.read() is None
        monkeypatch.setattr(ollama, "installed_names", lambda *_args, **_kwargs: set())
        model_recovery.check()
        assert model_recovery.read()["inventory"]["ollama"] == ["llama3.2:3b"]


def test_recovery_does_not_offer_a_pending_deletion(app, fake_ollama, monkeypatch):
    from bananachat.db import catalog
    from bananachat.services import model_recovery, ollama

    sync(app)
    publish(app, "llama3.2:3b")
    with app.app_context():
        row = catalog.get_by_name("llama3.2:3b")
        catalog.set_lifecycle(row["id"], delete_requested_at="2020-01-01 00:00:00")
        monkeypatch.setattr(ollama, "installed_names", lambda *_args, **_kwargs: set())
        model_recovery.check()
        assert model_recovery.read() is None


def test_failed_listing_defers_old_missing_catalog_purge(app, fake_ollama):
    from bananachat.db import catalog
    from bananachat.services import model_lifecycle
    from bananachat.services.upstream import UpstreamError

    sync(app)
    row = model(app, "llama3.2:3b")
    with app.app_context():
        catalog.set_lifecycle(row["id"], backend_available=0, missing_at="2000-01-01 00:00:00")
    fake_ollama.tags_status = 503
    with pytest.raises(UpstreamError):
        sync(app)
    with app.app_context():
        assert model_lifecycle.purge_missing() == 0
        assert catalog.get(row["id"]) is not None


@pytest.mark.parametrize("clear", ["one", "all", "legacy"])
def test_cancelled_discovery_survives_history_clear_until_verified_retry(app, fake_ollama, clear):
    from bananachat import db
    from bananachat.db import pulls
    from bananachat.db import settings

    fake_ollama.models = ["cancelled:latest"]
    with app.app_context():
        job = pulls.enqueue("cancelled", None)
        assert pulls.cancel(job)
        if clear == "legacy":
            settings.state_delete(pulls.CANCELLED_DISCOVERY_KEY)
        if clear == "one":
            assert pulls.delete_finished(job)
        else:
            assert pulls.clear_finished() == 1
    sync(app)
    assert model(app, "cancelled:latest") is None
    with app.app_context():
        retry = pulls.enqueue("cancelled:latest", None)
        claimed = pulls.claim_next()
        assert claimed["id"] == retry
        assert pulls.finish(retry, "done", digest=fake_ollama.digest("cancelled:latest"), owner=claimed["claim_token"])
        assert db.scalar("SELECT status FROM model_pull_jobs WHERE id=?", (retry,)) == "done"
    sync(app)
    assert model(app, "cancelled:latest")["enrollment"] == "new"


def test_aliases_cannot_duplicate_downloads_or_bypass_delete_guard(app, fake_ollama):
    from bananachat.db import catalog, pulls as pulls_db
    from bananachat.services import pulls

    with app.app_context():
        first = pulls.enqueue_ollama("alias", None)
        with pytest.raises(pulls_db.AlreadyActive):
            pulls.enqueue_ollama("alias:latest", None)
        assert pulls_db.get(first)["ollama_name"] == "alias:latest"
    fake_ollama.models = ["blocked:latest"]
    sync(app)
    with app.app_context():
        row = catalog.get_by_name("blocked:latest")
        catalog.set_lifecycle(row["id"], delete_requested_at="2020-01-01 00:00:00")
        with pytest.raises(ValueError, match="being deleted"):
            pulls.enqueue_ollama("blocked", None)
