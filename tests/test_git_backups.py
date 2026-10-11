"""Real age encryption and disposable Git remotes; no live forge or user data."""

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.error

import pytest

from banana_backup import crypto
from banana_backup import git as transport
from banana_backup import store as storage
from banana_backup.files import atomic_write, digest, write_json
from banana_backup.store import Store


@pytest.fixture
def key(tmp_path):
    if not shutil.which("age") or not shutil.which("age-keygen"):
        pytest.fail("Install age to run the encrypted backup tests.")
    path = tmp_path / "recovery.agekey"
    crypto.keygen(path)
    return path


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(path)], capture_output=True, check=True)
    return path


@pytest.fixture
def store(tmp_path, key, remote, monkeypatch):
    monkeypatch.setattr(storage, "check_private", lambda *_: True)
    @contextmanager
    def local_transport(self, config):
        with tempfile.TemporaryDirectory(prefix="transfer-", dir=self.root) as name:
            yield transport.Git(Path(name), {**config, "url": str(remote)}, self.token, allow_local=True)
    monkeypatch.setattr(Store, "transport", local_transport)
    token = tmp_path / "token"
    atomic_write(token, "fixture-backup-token")
    result = Store(tmp_path / "config", "BananaWiki")
    result.configure(repo="https://forge.example/owner/backups.git", forge="forgejo", name="wiki-production",
                     token_file=token, key_file=key, max_mib=1, keep=2)
    return result


def command(remote, *args):
    return subprocess.run(["git", "--git-dir", str(remote), *args], capture_output=True, check=True).stdout


def package(tmp_path, value=b"private wiki database and attachments"):
    path = tmp_path / "fixture.tar.gz"
    path.write_bytes(value)
    return path


def test_roundtrip_is_encrypted_verified_and_independent_of_history(store, remote, tmp_path):
    source = package(tmp_path)
    result = store.upload(source)
    assert result["verified"] and result["sha256"] == digest(source)
    assert store.schedule()["enabled"] is False
    ref = store.prefix(store.settings()) + result["snapshot"]
    assert command(remote, "rev-list", "--count", ref).strip() == b"1"
    names = command(remote, "ls-tree", "--name-only", ref).decode().splitlines()
    assert names == ["index.json", "part-00000.age"]
    for name in names:
        assert source.read_bytes() not in command(remote, "show", ref + ":" + name)
        assert b"AGE-SECRET-KEY" not in command(remote, "show", ref + ":" + name)
        assert b"fixture-backup-token" not in command(remote, "show", ref + ":" + name)
    restored = tmp_path / "restored.tar.gz"
    store.download(result["snapshot"], restored)
    assert restored.read_bytes() == source.read_bytes()
    assert restored.stat().st_mode & 0o777 == 0o600
    assert store.verify(result["snapshot"])["verified"]


def test_retention_keeps_other_series_and_source_branches(store, remote, tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from banana_backup import store as storage
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(storage, "datetime", FixedClock)
    first = store.upload(package(tmp_path))
    command(remote, "update-ref", "refs/heads/main", first["commit"])
    other = "refs/heads/banana-backups/bananachat/another/" + first["snapshot"]
    command(remote, "update-ref", other, first["commit"])
    second = store.upload(package(tmp_path, b"second"))
    third = store.upload(package(tmp_path, b"third"))
    assert {item["snapshot"] for item in store.list()} == {second["snapshot"], third["snapshot"]}
    assert command(remote, "rev-parse", "main").strip().decode() == first["commit"]
    assert command(remote, "rev-parse", other).strip().decode() == first["commit"]
    assert store.verify(second["snapshot"])["verified"]


def test_wrong_key_and_snapshot_replay_publish_nothing(store, tmp_path):
    result = store.upload(package(tmp_path))
    crypto.keygen(tmp_path / "other.agekey")
    atomic_write(store.identity, (tmp_path / "other.agekey").read_bytes())
    target = tmp_path / "restored.tar.gz"
    with pytest.raises(ValueError, match="authentication"):
        store.download(result["snapshot"], target)
    assert not target.exists()
    with pytest.raises(ValueError, match="different recovery key"):
        store.upload(package(tmp_path))
    assert len(store.list()) == 1


def test_renamed_backup_fails_authenticated_snapshot_binding(store, remote, tmp_path):
    result = store.upload(package(tmp_path))
    fake_id = "20000101T000000Z-11111111"
    with store.transport(store.settings()) as git:
        entries = git.fetch(store.prefix(store.settings()) + result["snapshot"], result["commit"])
        index = json.loads(git.run("cat-file", "blob", entries["index.json"][0]))
        index["snapshot"] = fake_id
        index_path = git.work / "index.json"
        write_json(index_path, index)
        part = git.work / "part-00000.age"
        atomic_write(part, git.run("cat-file", "blob", entries[part.name][0]))
        git.push(git.commit([index_path, part]), store.prefix(store.settings()) + fake_id)
        # Without the (no longer matching) tag, as if it predated authentication.
        del index["authentication"]
        stripped = "20000101T000000Z-22222222"
        index["snapshot"] = stripped
        write_json(index_path, index)
        git.push(git.commit([index_path, part]), store.prefix(store.settings()) + stripped)
    target = tmp_path / "restored.tar.gz"
    with pytest.raises(ValueError, match="authentication failed"):
        store.download(fake_id, target)
    # The binding inside the ciphertext still holds when an operator accepts an unauthenticated snapshot.
    with pytest.raises(ValueError, match="different product, series, or snapshot"):
        store.download(stripped, target, allow_unauthenticated=True)
    assert not target.exists()


def test_tampering_fails_even_if_attacker_rewrites_public_checksums(store, remote, tmp_path):
    result = store.upload(package(tmp_path))
    ref = store.prefix(store.settings()) + result["snapshot"]
    with store.transport(store.settings()) as git:
        entries = git.fetch(ref, result["commit"])
        part = git.work / "part-00000.age"
        raw = bytearray(git.run("cat-file", "blob", entries[part.name][0]))
        raw[-1] ^= 1
        atomic_write(part, raw)
        index = json.loads(git.run("cat-file", "blob", entries["index.json"][0]))
        index["parts"][0]["sha256"] = digest(part)
        index_path = git.work / "index.json"
        write_json(index_path, index)
        sha = git.commit([part, index_path])
        git.run("push", "--force-with-lease=" + ref + ":" + result["commit"], git.url, sha + ":" + ref)
    with pytest.raises(ValueError, match="authentication"):
        store.download(result["snapshot"], tmp_path / "restored.tar.gz")
    assert not (tmp_path / "restored.tar.gz").exists()


def test_multiple_parts_restore_in_order(store, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PART_BYTES", 1024)
    source = package(tmp_path, bytes(range(256)) * 20)
    result = store.upload(source)
    destination = tmp_path / "restored.tar.gz"
    store.download(result["snapshot"], destination)
    assert destination.read_bytes() == source.read_bytes()


def test_failed_remote_verification_does_not_prune(store, tmp_path, monkeypatch):
    source = package(tmp_path)
    first = store.upload(source)
    second = store.upload(source)
    monkeypatch.setattr(store, "_download", lambda *_: (_ for _ in ()).throw(ValueError("fixture verification failure")))
    with pytest.raises(ValueError, match="fixture verification"):
        store.upload(source)
    assert {first["snapshot"], second["snapshot"]} <= {item["snapshot"] for item in store.list()}
    assert store.status()["last_backup"]["snapshot"] == second["snapshot"]


def test_opt_out_during_encryption_prevents_new_snapshot(store, tmp_path, monkeypatch):
    source = package(tmp_path)
    store.set_schedule(True)
    encrypt = crypto.encrypt
    def disabled(*args):
        result = encrypt(*args)
        store.set_schedule(False)
        return result
    monkeypatch.setattr(crypto, "encrypt", disabled)
    assert store.upload(source, automatic=True)["outcome"] == "disabled"
    assert store.list() == []


def test_existing_output_and_size_limit_are_preserved(store, tmp_path):
    source = package(tmp_path)
    result = store.upload(source)
    target = tmp_path / "existing.tar.gz"
    target.write_bytes(b"keep me")
    with pytest.raises(ValueError, match="new download filename"):
        store.download(result["snapshot"], target)
    assert target.read_bytes() == b"keep me"
    source.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="size limit"):
        store.upload(source)
    assert len(store.list()) == 1


@pytest.mark.parametrize("url", ["http://github.com/a/b", "https://secret@github.com/a/b", "file:///tmp/repo",
                                 "https://github.com/a/../b", "https://github.com/a/b?token=secret", "ssh://git@github.com/a/b"])
def test_backup_urls_cannot_carry_credentials_or_bypass_https(url):
    with pytest.raises(ValueError):
        transport.location(url, "github")


@pytest.mark.parametrize("forge,url,api", [
    ("github", "https://github.com/team/backups.git", "https://api.github.com/repos/team/backups"),
    ("forgejo", "https://git.example/forge/team/backups.git", "https://git.example/forge/api/v1/repos/team/backups"),
])
def test_private_visibility_is_required_and_api_auth_stays_on_origin(tmp_path, monkeypatch, forge, url, api):
    token = tmp_path / "token"
    atomic_write(token, "fixture-secret")
    response = {"private": True, "full_name": "team/backups"}
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self, _):
            return json.dumps(response).encode()
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == api
            assert request.get_header("Authorization") == ("Bearer " if forge == "github" else "token ") + "fixture-secret"
            return Response()
    monkeypatch.setattr(transport.urllib.request, "build_opener", lambda *_: Opener())
    config = {"url": url, "forge": forge}
    assert transport.check_private(config, token)
    response["private"] = False
    with pytest.raises(ValueError, match="private repository"):
        transport.check_private(config, token)
    assert transport.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.example") is None


def test_keys_tokens_and_working_directories_are_private(store, key, tmp_path):
    assert store.identity.stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.identity.read_text() not in json.dumps(store.status())
    key.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        crypto.recipient(key)
    linked = tmp_path / "linked.key"
    linked.symlink_to(store.identity)
    with pytest.raises(OSError):
        crypto.recipient(linked)


def test_backup_auth_ignores_global_git_and_environment_credentials(store, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "malicious-config"))
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    monkeypatch.setenv("GIT_SSH_COMMAND", "must-not-execute")
    with store.transport(store.settings()) as git:
        assert git.environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert "GITHUB_TOKEN" not in git.environment
        assert "GIT_SSH_COMMAND" not in git.environment
        assert "fixture-backup-token" not in str(git.environment)
        assert "fixture-backup-token" not in str(git.options)


def test_backup_git_errors_are_diagnosable_without_repeating_credentials(tmp_path, monkeypatch):
    token = tmp_path / "private" / "token"
    atomic_write(token, "fixture-backup-token")
    missing = tmp_path / "missing-remote.git"
    git = transport.Git(tmp_path / "work", {"url": str(missing), "max_mib": 1}, token, allow_local=True)
    with pytest.raises(RuntimeError, match="Git reported: .*missing-remote"):
        git.heads("refs/heads/banana-backups/")
    calls = []

    def echoed(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 128, b"", b"fatal: https://bot:fixture-backup-token@forge.example denied\n")

    monkeypatch.setattr(transport.subprocess, "run", echoed)
    with pytest.raises(RuntimeError) as error:
        git.push("a" * 40, "refs/heads/banana-backups/x")
    assert "denied" in str(error.value) and "fixture-backup-token" not in str(error.value)
    # A URL or ref is never where git expects an option.
    assert calls[-1][calls[-1].index("--"):] == ["--", str(missing), "a" * 40 + ":refs/heads/banana-backups/x"]
    with pytest.raises(ValueError):
        transport.Git(tmp_path / "other", {"url": "--upload-pack=touch", "max_mib": 1}, token, allow_local=True)


def forge(store, tmp_path, *, authentication=None, identifier="20260930T000000.000000Z-deadbeef"):
    """What anyone who can push to the backup repository can do, knowing only the public recipient.

    It is also what a snapshot uploaded before snapshots were authenticated looks like.
    """
    import hashlib
    config = store.settings()
    context = store.context(config, identifier)
    payload = b"a package whose source.tar.gz and app.env a restore would deploy as root"
    header = {**context, "schema": 1, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    with store.transport(config) as git:
        part = git.work / "part-00000.age"
        subprocess.run(["age", "--encrypt", "--recipient", config["recipient"], "--output", str(part)],
                       input=crypto.MAGIC + json.dumps(header, sort_keys=True).encode() + b"\n" + payload, check=True)
        index = {**context, "schema": 1, "parts": [{"name": part.name, "bytes": part.stat().st_size, "sha256": digest(part)}]}
        if authentication is not None:
            index["authentication"] = authentication
        write_json(git.work / "index.json", index)
        git.push(git.commit([part, git.work / "index.json"]), store.prefix(config) + identifier)
    return identifier


def backup_arguments(*argv):
    import argparse
    from banana_backup import cli
    parser = argparse.ArgumentParser()
    cli.add_commands(parser.add_subparsers(dest="command"))
    return cli, parser.parse_args(["backups", *argv])


def test_only_a_restore_that_deploys_takes_allow_unpinned():
    """The operator's explicit choice for a 1.7 or later release without its dependency locks (OPS2-09), as on
    restore and update; downloading, verifying and an agent's restore into a directory deploy nothing."""
    import argparse
    from banana_backup import cli

    assert backup_arguments("restore", "snapshot")[1].allow_unpinned is False
    assert backup_arguments("restore", "snapshot", "--allow-unpinned")[1].allow_unpinned is True
    for agent in (False, True):
        parser = argparse.ArgumentParser()
        cli.add_commands(parser.add_subparsers(dest="command"), agent=agent)
        commands = [("download", "--output", "x"), ("verify",)] + [("restore", "--output", "x")] * agent
        for action, *extra in commands:
            assert "allow_unpinned" not in vars(parser.parse_args(["backups", action, "snapshot", *extra]))


def test_a_snapshot_forged_with_the_public_recipient_is_refused(store, tmp_path):
    identifier = forge(store, tmp_path)
    assert identifier in {item["snapshot"] for item in store.list()}
    target = tmp_path / "restored.tar.gz"
    with pytest.raises(ValueError, match="not authenticated"):
        store.download(identifier, target)
    with pytest.raises(ValueError, match="not authenticated"):
        store.verify(identifier)
    assert not target.exists()


def test_a_genuine_tag_does_not_cover_a_forged_index(store, remote, tmp_path):
    genuine = store.upload(package(tmp_path))
    index = json.loads(command(remote, "show", store.prefix(store.settings()) + genuine["snapshot"] + ":index.json"))
    assert index["authentication"]["scheme"] == crypto.AUTHENTICATION
    identifier = forge(store, tmp_path, authentication=index["authentication"])
    for allow in (False, True):
        with pytest.raises(ValueError, match="authentication failed"):
            store.download(identifier, tmp_path / f"restored-{allow}.tar.gz", allow_unauthenticated=allow)


def test_unauthenticated_snapshots_restore_only_with_the_explicit_opt_in(store, tmp_path, capsys):
    """Snapshots from before authentication look exactly like a forgery; the operator decides, loudly warned."""
    identifier = forge(store, tmp_path)
    restored = []

    def restore_package(path, options):
        restored.append(Path(path).read_bytes())
        return {"outcome": "complete"}

    cli, args = backup_arguments("restore", identifier)
    with pytest.raises(ValueError, match="--allow-unauthenticated"):
        cli.handle(args, store, create_package=None, restore_package=restore_package)
    assert restored == []
    cli, args = backup_arguments("restore", identifier, "--allow-unauthenticated")
    assert cli.handle(args, store, create_package=None, restore_package=restore_package) == {"outcome": "complete"}
    assert restored and restored[0].startswith(b"a package")
    assert "WARNING" in capsys.readouterr().err
    verified = store.verify(identifier, allow_unauthenticated=True)
    assert verified["verified"] and verified["authenticated"] is False


def test_the_authentication_key_comes_from_the_recovery_identity(store, tmp_path):
    """A full-server-loss restore needs only the recovery key the operator saved offline."""
    identifier = store.upload(package(tmp_path))["snapshot"]
    assert store.verify(identifier)["authenticated"] is True
    fresh = Store(tmp_path / "new-server", store.product)
    fresh.root.mkdir(mode=0o700)
    for name in ("repository.json", "repo.token", "recovery.agekey"):
        atomic_write(fresh.root / name, (store.root / name).read_bytes())
    assert fresh.verify(identifier)["authenticated"] is True
    crypto.keygen(tmp_path / "other.agekey")
    atomic_write(fresh.identity, (tmp_path / "other.agekey").read_bytes())
    with pytest.raises(ValueError, match="authentication failed"):
        fresh.verify(identifier)


def test_append_only_series_never_delete_snapshots(store, remote, tmp_path):
    """The token then needs no permission to delete branches; pruning is a separate, explicit step."""
    cli, args = backup_arguments("configure", "--repo", "https://forge.example/owner/backups.git", "--forge", "forgejo",
                                 "--name", "wiki-production", "--token-file", str(store.token),
                                 "--key-file", str(store.identity), "--keep", "2", "--max-mib", "1", "--append-only")
    assert cli.handle(args, store, create_package=None, restore_package=None)["repository"]["append_only"] is True
    removed = []
    real_remove = transport.Git.remove
    transport.Git.remove = lambda self, *refs: removed.append(refs)
    try:
        identifiers = [store.upload(package(tmp_path, bytes([number])))["snapshot"] for number in range(3)]
    finally:
        transport.Git.remove = real_remove
    assert removed == []
    assert {item["snapshot"] for item in store.list()} == set(identifiers)
    privileged = tmp_path / "delete.token"
    atomic_write(privileged, "fixture-delete-token")
    cli, args = backup_arguments("prune", "--token-file", str(privileged))
    result = cli.handle(args, store, create_package=None, restore_package=None)
    assert result["removed"] == [identifiers[0]] and not result["warnings"]
    assert [item["snapshot"] for item in store.list()] == identifiers[:0:-1]
    assert "fixture-delete-token" not in (store.root / "repo.token").read_text()
