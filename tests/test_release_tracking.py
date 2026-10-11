"""What automatic updates deploy: release tags by default, and only signed revisions unless the operator opts out.

Branch HEAD is whatever was pushed last, work in progress included; a release
tag (vX.Y.Z) is what the maintainers released. Automatic updates run the
fetched code as root, so turning them on requires signature checks
(--require-signatures) unless the operator explicitly accepts unsigned
revisions (--allow-unsigned).
"""

import shutil
import subprocess

import pytest

from banana_ops import cli
from banana_ops.files import read_json, write_json
from banana_ops.manager import Manager
from banana_ops.source import GitSource
from test_managed_lifecycle import Services, checkout, git, installed, revision  # noqa: F401

needs_ssh_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def tag(source, name, *options):
    git("-C", source, "tag", *options, name)


def tags_source(tmp_path, checkout, **extra):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True, exist_ok=True)
    return GitSource(root, {"url": str(checkout), "branch": "main", "track": "tags", **extra})


def run_cli(monkeypatch, manager, *arguments):
    monkeypatch.setattr(cli, "Manager", lambda _: manager)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    return cli.main(["--root", str(manager.root), *arguments])


def test_new_sources_track_release_tags_and_existing_ones_keep_their_branch(tmp_path):
    manager = Manager(tmp_path / "managed", product="BananaChat", system=Services())
    manager.layout()
    assert manager.configure_source()["track"] == "tags"
    assert manager.configure_source(track="branch")["track"] == "branch"
    with pytest.raises(ValueError, match="track"):
        manager.configure_source(track="nightly")
    # A source configured before 1.7 says nothing: it keeps following its branch.
    write_json(manager.config_dir / "source.json", {"url": "https://example.org/r.git", "branch": "main",
                                                    "fallback_branch": None, "auth": "none", "signing": "none"})
    assert manager.source()["track"] == "branch"
    assert "track" not in manager.configure_source(fallback="stable")


def test_tags_follow_the_highest_release_not_the_branch_head(tmp_path, checkout):
    first = git("-C", checkout, "rev-parse", "HEAD")
    tag(checkout, "v1.6.0")
    revision(checkout, "1.9.0")
    tag(checkout, "v1.9.0")
    release = revision(checkout, "1.10.0")
    tag(checkout, "v1.10.0", "-a", "-m", "BananaChat 1.10.0")  # annotated: resolved to its commit
    head = revision(checkout, "work in progress")
    for name in ("v2.0.0-rc1", "latest", "v02.0.0"):
        tag(checkout, name)
    source = tags_source(tmp_path, checkout)
    assert source.resolve(first) == (release, "v1.10.0", True)
    # A server installed from a newer checkout (its cache holds that revision) already has the release.
    GitSource(source.root, {**source.source, "track": "branch"}).resolve(first)
    assert source.resolve(head) == (head, "v1.10.0", True)


def test_without_any_release_tag_updates_wait_and_say_how_to_follow_a_branch(tmp_path, checkout):
    with pytest.raises(RuntimeError, match="--track branch"):
        tags_source(tmp_path, checkout).resolve(git("-C", checkout, "rev-parse", "HEAD"))


@needs_ssh_keygen
def test_a_release_tag_signed_for_its_name_is_required_when_signatures_are(tmp_path, checkout):
    key, stranger = tmp_path / "maintainer", tmp_path / "stranger"
    for path in (key, stranger):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    first = git("-C", checkout, "rev-parse", "HEAD")
    tag(checkout, "v1.0.0")
    source = tags_source(tmp_path, checkout, signing="ssh")
    signers = source.root / "config/repo.allowed_signers"
    signers.write_text("maintainer@example.org " + (tmp_path / "maintainer.pub").read_text())
    signers.chmod(0o600)
    with pytest.raises(RuntimeError, match="not a signed tag"):
        source.resolve(first)
    release = revision(checkout, "unsigned commit, signed release")
    git("-C", checkout, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "tag", "-s", "-m", "1.1.0", "v1.1.0")
    assert source.resolve(first) == (release, "v1.1.0", True) and source.skipped_releases == []
    # The signed tag object only vouches for its own name: copied to v9.0.0 it releases nothing, and the highest
    # release that verifies is deployed instead.
    git("-C", checkout, "update-ref", "refs/tags/v9.0.0", "refs/tags/v1.1.0")
    assert source.resolve(first) == (release, "v1.1.0", True) and source.skipped_releases == ["v9.0.0"]
    git("-C", checkout, "tag", "-d", "v9.0.0")
    revision(checkout, "1.2.0")
    git("-C", checkout, "-c", "gpg.format=ssh", "-c", f"user.signingkey={stranger}", "tag", "-s", "-m", "1.2.0", "v1.2.0")
    assert source.resolve(first) == (release, "v1.1.0", True) and source.skipped_releases == ["v1.2.0"]
    # A commit signed by an allowed key under a tag anyone could push releases nothing either.
    git("-C", checkout, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "commit", "-S", "--allow-empty",
        "-m", "signed work in progress")
    tag(checkout, "v1.3.0")
    assert source.resolve(first) == (release, "v1.1.0", True)
    assert source.skipped_releases == ["v1.3.0", "v1.2.0"]


def test_an_update_deploys_the_latest_release(tmp_path, checkout):
    manager = Manager(tmp_path / "managed", product="BananaChat", system=Services())
    model = tmp_path / "ollama"
    model.write_text("fixture")
    manager.install(checkout, mode="single", ollama_binary=str(model),
                    source_options={"url": str(checkout), "allow_local": True})
    assert manager.source()["track"] == "tags"  # what a new installation follows
    release = revision(checkout, "1.7.0")
    tag(checkout, "v1.7.0")
    revision(checkout, "unreleased work")
    result = manager.update()
    assert result["outcome"] == "complete" and result["revision"] == release and result["release"] == "v1.7.0"
    assert "used_fallback" not in result
    assert manager.update()["outcome"] == "current"


def test_enabling_automatic_updates_requires_signatures_unless_explicitly_waived(tmp_path, checkout, monkeypatch,
                                                                                   capsys):
    manager, _services = installed(tmp_path, checkout, product="BananaChat", mode="single")
    with pytest.raises(ValueError, match="--require-signatures"):
        manager.set_updates(True)
    assert run_cli(monkeypatch, manager, "updates", "enable") == 1
    assert "--require-signatures" in capsys.readouterr().err and manager.policy()["enabled"] is False
    assert run_cli(monkeypatch, manager, "updates", "enable", "--allow-unsigned") == 0
    assert manager.policy()["enabled"] is True and manager.policy()["unsigned"] is True
    assert "unsigned" in capsys.readouterr().err.lower()
    assert manager.status()["warnings"]
    run_cli(monkeypatch, manager, "updates", "disable")
    signers = tmp_path / "allowed_signers"
    signers.write_text("maintainer@example.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample\n")
    assert run_cli(monkeypatch, manager, "updates", "enable", "--require-signatures", str(signers)) == 0
    assert manager.source()["signing"] == "ssh" and manager.policy()["unsigned"] is False
    assert "warnings" not in manager.status()
    # Turning the checks off later needs the same explicit waiver while automatic updates are on.
    assert run_cli(monkeypatch, manager, "source", "set", "--clear-signatures") == 1
    assert manager.source()["signing"] == "ssh"
    assert run_cli(monkeypatch, manager, "source", "set", "--clear-signatures", "--allow-unsigned") == 0
    assert manager.source()["signing"] == "none"
    assert read_json(manager.config_dir / "updates.json")["unsigned"] is True
