"""Findings of ruff's security rules (flake8-bandit) that were real problems, not false positives.

``urllib`` opens ``file:`` and other schemes as readily as HTTP, so an address
that is meant to be an HTTP service is checked before it is opened, and
exceptions are logged instead of silently dropped.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

import pytest

from banana_ops import models

ROOT = Path(__file__).resolve().parents[1]


def _ollama_files(tmp_path):
    """A directory that answers like Ollama's /api/tags when opened as a file: URL."""
    (tmp_path / "api").mkdir(parents=True)
    (tmp_path / "api" / "tags").write_text(json.dumps({"models": [{"name": "local-secret", "size": 1}]}))
    return "file://" + str(tmp_path)


def test_model_listing_never_opens_a_file_url(tmp_path):
    assert models._listed_models(_ollama_files(tmp_path)) is None


def test_model_restore_refuses_an_address_that_is_not_http(tmp_path):
    (tmp_path / models.RECOVERY_FILE).write_text(json.dumps(
        {"inventory": {"ollama": ["llama3.2:3b"], "manual": []}, "state": "pending"}))
    with pytest.raises(ValueError, match="http"):
        models.compute_recovery(tmp_path, "restore", assume_yes=True, upstream=_ollama_files(tmp_path / "x"))


def test_load_test_requests_only_http_addresses():
    spec = importlib.util.spec_from_file_location("load_test_for_lint", ROOT / "scripts" / "load_test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match="http"):
        module.request("file:///etc/hostname")


def test_a_failing_cancel_callback_is_logged_and_the_others_still_run(caplog):
    from bananachat.services.upstream import CancelToken

    token, ran = CancelToken(), []

    def broken():
        raise RuntimeError("callback bug")

    token.on_cancel(broken)
    token.on_cancel(lambda: ran.append(True))
    with caplog.at_level(logging.WARNING, logger="bananachat.upstream"):
        token.cancel("stopped")
    assert ran == [True] and token.reason == "stopped"
    assert any("callback bug" in (record.exc_text or "") or record.exc_info for record in caplog.records)
