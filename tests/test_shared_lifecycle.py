"""Maintenance checks prevent unnoticed drift or overwriting local changes."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sync_lifecycle.py"
SPEC = importlib.util.spec_from_file_location("sync_lifecycle", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def checkout(root, name):
    """Create an independent fixture with only managed common files and identity."""
    root.mkdir()
    for relative in sync.FIXED_FILES | {
        "banana_ops/example.py",
        "banana_ops/product.py",
    }:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'PRODUCT = "{name}"\n'
            if path.name == "product.py"
            else SCRIPT.read_text() if relative == "scripts/sync_lifecycle.py"
            else "# original fixture\n"
        )
    sync.record(root)
    return root


def test_shared_change_requires_review(tmp_path):
    """CI rejects changed and newly added common code until deliberately recorded."""
    root = checkout(tmp_path / "wiki", "BananaWiki")
    (root / "banana_ops/example.py").write_text("# new fixture\n")
    with pytest.raises(ValueError, match="example.py"):
        sync.check(root)
    sync.record(root)
    sync.check(root)
    (root / "banana_ops/extra.py").write_text("# added file\n")
    with pytest.raises(ValueError, match="extra.py"):
        sync.check(root)


def test_sync_preserves_product_and_unrelated_data(tmp_path):
    """Both products get identical common changes without copying identity or data."""
    source = checkout(tmp_path / "wiki", "BananaWiki")
    target = checkout(tmp_path / "ai", "BananaChat")
    (target / "private.txt").write_text("fixture data")
    (source / "banana_ops/example.py").write_text("# reviewed change\n")
    sync.record(source)
    sync.synchronize(source, target)
    assert sync.check(source) == sync.check(target)
    assert sync.product(target) == "BananaChat"
    assert (target / "private.txt").read_text() == "fixture data"


def test_sync_refuses_unrecorded_destination_edits(tmp_path):
    """A destination's unfinished local work is never overwritten by synchronization."""
    source = checkout(tmp_path / "wiki", "BananaWiki")
    target = checkout(tmp_path / "ai", "BananaChat")
    path = target / "banana_ops/example.py"
    path.write_text("# unfinished work\n")
    with pytest.raises(ValueError, match="example.py"):
        sync.synchronize(source, target)
    assert path.read_text() == "# unfinished work\n"


def test_linked_destination_is_rejected_before_writing(tmp_path):
    """A linked shared file cannot redirect the maintenance copy outside the target."""
    source = checkout(tmp_path / "wiki", "BananaWiki")
    target = checkout(tmp_path / "ai", "BananaChat")
    outside = tmp_path / "outside.py"
    outside.write_text("# private fixture\n")
    path = target / "banana_ops/example.py"
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic links"):
        sync.synchronize(source, target)
    assert outside.read_text() == "# private fixture\n"


def test_removed_common_file_is_removed_on_sync(tmp_path):
    """An intentionally retired common file is removed from the verified copy."""
    source = checkout(tmp_path / "wiki", "BananaWiki")
    target = checkout(tmp_path / "ai", "BananaChat")
    (source / "banana_ops/example.py").unlink()
    sync.record(source)
    sync.synchronize(source, target)
    assert not (target / "banana_ops/example.py").exists()
    assert json.loads((target / sync.MANIFEST).read_text()) == sync.check(source)


def test_sync_can_add_a_new_fixed_file_to_an_older_recorded_copy(tmp_path):
    source = checkout(tmp_path / "wiki", "BananaWiki")
    target = checkout(tmp_path / "ai", "BananaChat")
    old_tool = target / "scripts/sync_lifecycle.py"
    old_tool.write_text(old_tool.read_text().replace('    "sqlite_runtime.py",\n', ''))
    (target / "sqlite_runtime.py").unlink()
    sync.record(target)
    sync.synchronize(source, target)
    assert (target / "sqlite_runtime.py").read_text() == (source / "sqlite_runtime.py").read_text()
    assert sync.check(target) == sync.check(source)
