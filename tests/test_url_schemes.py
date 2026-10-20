"""Addresses meant for HTTP services refuse other schemes.

``urllib`` opens ``file:`` and other schemes as readily as HTTP, so each
address is checked before it is opened.
"""

from __future__ import annotations

import importlib.util
import json
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
    spec = importlib.util.spec_from_file_location("load_test_module", ROOT / "scripts" / "load_test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match="http"):
        module.request("file:///etc/hostname")

