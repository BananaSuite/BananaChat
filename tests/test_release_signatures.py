"""Signed releases on servers that require signatures: what updates deploy and what restores accept.

* A release is an annotated tag signed for its own name (``git tag -s``). A
  signed commit under a tag anyone could push says nothing about which
  version it is, so it releases nothing.
* A higher tag that does not verify is skipped, never a reason to stop
  updating.
* A restore deploys a revision the update source released under such a tag,
  as the update did, also once its release directory was pruned, and keeps
  this server's update source and signature trust.
"""

import os
import shutil
import subprocess

import pytest

from banana_ops.files import write_json
from banana_ops.source import GitSource
from test_managed_lifecycle import Services, checkout, git, installed, revision  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture
def keys(tmp_path):
    key = tmp_path / "maintainer"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    signers = tmp_path / "allowed_signers"
    signers.write_text("maintainer@example.org " + key.with_suffix(".pub").read_text())
    signers.chmod(0o600)
    return key, signers


@pytest.fixture
def keyring(tmp_path, monkeypatch):
    """A GnuPG keyring the updater consults, holding a key that is not an allowed signer."""
    if shutil.which("gpg") is None:
        pytest.skip("needs gpg")
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    subprocess.run(["gpg", "--homedir", str(home), "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                    "--quick-gen-key", "Other <other@example.org>", "ed25519", "sign", "never"],
                   check=True, capture_output=True)
    environment = GitSource.environment
    monkeypatch.setattr(GitSource, "environment", lambda self: {**environment(self), "GNUPGHOME": str(home)})
    yield home
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], capture_output=True)


def openpgp(keyring, *args, **options):
    return subprocess.run(args, env={**os.environ, "GNUPGHOME": str(keyring)}, capture_output=True, text=True,
                          check=True, **options).stdout.strip()


def signed_tag(source, key, name):
    git("-C", source, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "tag", "-s", "-m", name, name)


def signed_server(tmp_path, checkout, signers):
    manager, _services = installed(tmp_path, checkout, "BananaChat", "single")
    manager.configure_source(track="tags", signers_file=signers)
    return manager


def test_a_signed_commit_under_a_tag_anyone_could_push_releases_nothing(tmp_path, checkout, keys):
    key, signers = keys
    manager = signed_server(tmp_path, checkout, signers)
    installed_revision = manager.settings()["revision"]
    signed_tag(checkout, key, "v1.7.0")
    git("-C", checkout, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "commit", "-S", "--allow-empty",
        "-m", "signed work in progress, not a release")
    git("-C", checkout, "tag", "v99.0.0")  # pushed by someone who can push tags but holds no signing key
    result = manager.update()
    assert result["outcome"] == "current" and result["release"] == "v1.7.0"
    assert result["skipped_releases"] == ["v99.0.0"]
    assert manager.settings()["revision"] == installed_revision


def test_an_unverifiable_higher_tag_does_not_hold_back_a_signed_release(tmp_path, checkout, keys, capsys):
    key, signers = keys
    manager = signed_server(tmp_path, checkout, signers)
    first = manager.settings()["revision"]
    revision(checkout, "junk")
    git("-C", checkout, "tag", "v999.0.0")
    git("-C", checkout, "checkout", "-q", "-b", "fix", first)
    fix = revision(checkout, "1.7.1 security fix")
    signed_tag(checkout, key, "v1.7.1")
    result = manager.update()
    assert result["outcome"] == "complete" and result["revision"] == fix and result["release"] == "v1.7.1"
    assert result["skipped_releases"] == ["v999.0.0"]
    assert "v999.0.0" in capsys.readouterr().err


def test_a_tag_signed_release_restores_once_its_release_directory_is_gone(tmp_path, checkout, keys):
    key, signers = keys
    manager = signed_server(tmp_path, checkout, signers)
    r170 = revision(checkout, "1.7.0")  # unsigned commits: the documented procedure signs the tag
    signed_tag(checkout, key, "v1.7.0")
    assert manager.update()["revision"] == r170
    package = manager.backup()
    r171 = revision(checkout, "1.7.1")
    signed_tag(checkout, key, "v1.7.1")
    assert manager.update()["revision"] == r171
    shutil.rmtree(manager.root / "releases" / r170)
    shutil.rmtree(manager.root / "repository.git")
    assert manager.restore(package)["outcome"] == "complete"
    assert manager.settings()["revision"] == r170


def test_a_restore_of_an_unsigned_revision_says_how_to_proceed(tmp_path, checkout, keys):
    key, signers = keys
    manager = signed_server(tmp_path, checkout, signers)
    unsigned = manager.settings()["revision"]
    package = manager.backup()
    newer = revision(checkout, "1.7.0")
    signed_tag(checkout, key, "v1.7.0")
    assert manager.update()["revision"] == newer
    shutil.rmtree(manager.root / "releases" / unsigned)
    for trust in (None, unsigned):
        with pytest.raises(ValueError) as error:
            manager.restore(package, trust_source=trust)
        message = str(error.value)
        assert unsigned in message and "--clear-signatures" in message and "--trust-source" in message
        assert "--require-signatures" in message and "Nothing was changed" in message
    assert manager.settings()["revision"] == newer


def test_a_restore_keeps_the_servers_update_source_and_signature_trust(tmp_path, checkout, keys):
    key, signers = keys
    manager = signed_server(tmp_path, checkout, signers)
    trusted = (manager.config_dir / "source.json").read_bytes()
    allowed = (manager.config_dir / "repo.allowed_signers").read_bytes()
    write_json(manager.config_dir / "source.json", {"url": "https://attacker.example/chat.git", "branch": "main",
                                                    "fallback_branch": None, "auth": "none", "signing": "none",
                                                    "track": "branch"})
    (manager.config_dir / "repo.allowed_signers").unlink()
    package = manager.backup()
    (manager.config_dir / "source.json").write_bytes(trusted)
    (manager.config_dir / "repo.allowed_signers").write_bytes(allowed)
    result = manager.restore(package)
    assert result["outcome"] == "complete"
    assert (manager.config_dir / "source.json").read_bytes() == trusted
    assert (manager.config_dir / "repo.allowed_signers").read_bytes() == allowed
    assert "attacker.example" in result["source_warning"] and "kept" in result["source_warning"]


def test_an_openpgp_signed_tag_quoting_an_ssh_signature_releases_nothing(tmp_path, checkout, keys, keyring):
    key, signers = keys
    manager = signed_server(tmp_path, checkout, signers)
    installed_revision = manager.settings()["revision"]
    signed_tag(checkout, key, "v1.7.0")
    revision(checkout, "pushed by the holder of a key this machine's GnuPG trusts")
    openpgp(keyring, "git", "-C", checkout, "-c", "gpg.format=openpgp", "-c", "user.signingkey=other@example.org",
            "tag", "-s", "-m", "v9.9.9\n\n-----BEGIN SSH SIGNATURE-----\n(quoted from the release notes)", "v9.9.9")
    result = manager.update()
    assert result["outcome"] == "current" and result["release"] == "v1.7.0"
    assert result["skipped_releases"] == ["v9.9.9"]
    assert manager.settings()["revision"] == installed_revision


def test_an_openpgp_signed_commit_with_an_ssh_signature_header_is_refused(tmp_path, checkout, keys, keyring):
    _key, signers = keys
    manager, _services = installed(tmp_path, checkout, "BananaChat", "single")
    manager.configure_source(signers_file=signers)
    installed_revision = manager.settings()["revision"]
    header = (f"tree {git('-C', checkout, 'rev-parse', 'HEAD^{tree}')}\nparent {installed_revision}\n"
              "author Other <other@example.org> 1767225600 +0000\ncommitter Other <other@example.org> 1767225600 +0000\n"
              "note -----BEGIN SSH SIGNATURE-----")
    message = "Pushed by the holder of a key this machine's GnuPG trusts\n"
    signature = openpgp(keyring, "gpg", "--batch", "--armor", "--detach-sign", "--local-user", "other@example.org",
                        input=header + "\n\n" + message)
    signed = header + "\ngpgsig " + signature.replace("\n", "\n ") + "\n\n" + message
    commit = openpgp(keyring, "git", "-C", checkout, "hash-object", "-t", "commit", "-w", "--literally", "--stdin",
                     input=signed)
    git("-C", checkout, "update-ref", "refs/heads/main", commit)
    # Without the format check, this machine's GnuPG would accept it.
    assert subprocess.run(["git", "-C", checkout, "verify-commit", commit], env={**os.environ, "GNUPGHOME": str(keyring)},
                          capture_output=True).returncode == 0
    with pytest.raises(RuntimeError, match="OpenPGP"):
        manager.update()
    assert manager.settings()["revision"] == installed_revision
