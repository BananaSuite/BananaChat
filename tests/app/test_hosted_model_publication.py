"""Hosted-model publication preserves budgets and ignores local GPU admission pressure."""
import pytest

from bananachat import db
from bananachat.db import catalog, external_providers as provider_store, limits as limits_db
from bananachat.services import external_providers, limits, model_lifecycle, queue


def external_model():
    provider_id = provider_store.save(name="Publication test", protocol="openai",
                                      base_url="https://provider.invalid/v1")
    return external_providers.enroll(provider_store.get(provider_id), "gpt-test", manual=True)


@pytest.mark.parametrize("explicit_current_preset", [False, True])
def test_review_and_republication_preserve_custom_hosted_model_limits(app, explicit_current_preset):
    with app.app_context():
        row = external_model()
        preset = model_lifecycle.preset_for(row)
        custom = {**limits_db.get_model_policy(row), "preset": "custom", "window_tokens": 1234,
                  "weekly_tokens": 9876, "weight": 2, "counts_toward_pool": True,
                  "rate_rules": [{"requests": 2, "per": "hour", "burst": 1}],
                  "effort_default": "low", "effort_gating_off": True}
        custom = limits_db.set_model_policy(row["id"], custom, None)
        model_lifecycle.enable(row, preset=preset if explicit_current_preset else None)
        catalog.set_lifecycle(row["id"], is_rolled_out=0)
        model_lifecycle.enable(catalog.get(row["id"]), preset=preset)
        assert limits_db.get_model_policy(catalog.get(row["id"])) == custom


def test_different_hosted_publication_preset_applies_its_new_budget(app):
    with app.app_context():
        row = external_model()
        model_lifecycle.enable(row, preset="heavy")
        after = limits_db.get_model_policy(catalog.get(row["id"]))
        assert after["enabled"] and after["window_tokens"] == 50_000


def test_hosted_publication_installs_limits_when_policy_is_missing(app):
    with app.app_context():
        row = external_model()
        limits_db.clear_model_policy(row["id"])
        model_lifecycle.enable(row)
        policy = limits_db.get_model_policy(catalog.get(row["id"]))
        assert policy["enabled"] and policy["window_tokens"] == 100_000


def test_hosted_model_uses_a_queue_slot_without_local_model_memory(app, monkeypatch):
    monkeypatch.setattr(queue, "_memory_ok", lambda: False)
    with app.app_context():
        row = external_model()
        assert queue._model_memory_ok(row["ollama_name"])
        assert not queue._model_memory_ok("unregistered-local-model")
        with queue.Slot(queue.PRIORITY_CHAT, model=row["ollama_name"]) as slot:
            assert slot._try_start()
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0


def test_hosted_request_can_run_while_local_queue_head_waits_for_memory(app, monkeypatch):
    monkeypatch.setattr(queue, "_memory_ok", lambda: False)
    with app.app_context():
        row = external_model()
        with queue.Slot(queue.PRIORITY_CHAT, model="local-queue-head") as local:
            with queue.Slot(queue.PRIORITY_CHAT, model=row["ollama_name"]) as hosted:
                assert not local._try_start()
                assert hosted._try_start()
                assert not local._try_start()
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0


def test_direct_limits_preset_uses_hosted_budget(app):
    with app.app_context():
        row = external_model()
        policy = limits.apply_model_preset(row["id"], "standard")
        assert policy["enabled"] and policy["window_tokens"] == 100_000
        assert policy["rate_rules"] and policy["dynamic"]


def test_admin_quota_preset_buttons_keep_hosted_model_limits(app, admin):
    with app.app_context():
        row = external_model()
        response = admin.post(f'/admin/quotas/models/{row["id"]}', {"action": "standard"})
        assert response.status_code == 302
        policy = limits_db.get_model_policy(catalog.get(row["id"]))
        assert policy["enabled"] and policy["window_tokens"] == 100_000
        assert policy["rate_rules"] and policy["dynamic"]
