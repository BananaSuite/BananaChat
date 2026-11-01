"""Regression tests for audit findings RC3-1 (tunnel relay greetings) and RC4-1 (backup tree listings)."""

from __future__ import annotations

import socket
import threading
import time

import pytest


# ----- RC3-1 -------------------------------------------------------------------

def _relay(monkeypatch, **limits):
    from bananachat.services import tunnel_broker

    for name, value in limits.items():
        monkeypatch.setattr(tunnel_broker, name, value)
    key = tunnel_broker._key("secret")
    broker = tunnel_broker.Broker(key)
    stop = threading.Event()
    threading.Thread(target=broker.serve, args=(stop.is_set,), daemon=True).start()
    return tunnel_broker, broker, key, stop


def _greeting_threads():
    return sum(1 for thread in threading.enumerate() if thread.name == "bananachat-tunnel-join")


def _closed(sock, wait=3.0):
    sock.settimeout(wait)
    try:
        return sock.recv(10) == b""
    except socket.timeout:
        return False
    except OSError:
        return True


def test_rc3_1_connections_that_never_authenticate_are_bounded(monkeypatch):
    tunnel_broker, broker, key, stop = _relay(monkeypatch, MAX_GREETINGS=4)
    before = _greeting_threads()
    strangers = []
    try:
        for _ in range(12):
            strangers.append(socket.create_connection(("127.0.0.1", broker.port), timeout=5))
        deadline = time.monotonic() + 3
        while _greeting_threads() - before < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert sum(_closed(sock, 0.2) for sock in strangers) >= 8  # those over the limit are closed at once
        assert _greeting_threads() - before <= 4
        for sock in strangers:
            sock.close()
        deadline = time.monotonic() + 3
        while _greeting_threads() > before and time.monotonic() < deadline:
            time.sleep(0.02)
        # Once they are gone, a real session still opens.
        from tests.app.test_worker_remote_access import _join

        worker = _join(broker.port, key, "worker", "a" * 32)
        client = _join(broker.port, key, "client", "a" * 32)
        assert tunnel_broker.wait_paired(worker, 5) and tunnel_broker.wait_paired(client, 5)
        worker.close()
        client.close()
    finally:
        stop.set()
        broker.close()
        for sock in strangers:
            sock.close()


def test_rc3_1_a_greeting_sent_byte_by_byte_has_one_deadline(monkeypatch):
    tunnel_broker, broker, key, stop = _relay(monkeypatch, GREETING_SECONDS=0.6)
    trickle = socket.create_connection(("127.0.0.1", broker.port), timeout=5)
    try:
        started = time.monotonic()
        closed = False
        for byte in b"BCT2 " + b"1" * 27:
            try:
                trickle.sendall(bytes([byte]))
            except OSError:
                closed = True
                break
            # Each byte comes well within any per-read timeout; only a deadline for the whole line ends it.
            trickle.settimeout(0.25)
            try:
                if trickle.recv(10) == b"":
                    closed = True
                    break
            except socket.timeout:
                pass
            except OSError:
                closed = True
                break
        assert closed and time.monotonic() - started < 3
    finally:
        trickle.close()
        stop.set()
        broker.close()


# ----- RC4-1 -------------------------------------------------------------------

REF = "refs/heads/banana-backups/bananachat/main/identity"


def _remote(tmp_path, entries):
    """A bare remote whose branch REF holds a tree built by *entries* (a function of the ``git`` helper)."""
    import os
    import subprocess

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    env = {**os.environ, "GIT_DIR": str(remote), "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b",
           "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b", "GIT_CONFIG_GLOBAL": "/dev/null"}

    def git(*args, data=None):
        return subprocess.run(["git", *args], input=data, env=env, capture_output=True, check=True).stdout.strip().decode()

    commit = git("commit-tree", git("mktree", data=entries(git).encode()), "-m", "x")
    git("update-ref", REF, commit)
    return remote, commit


def _client(tmp_path, remote):
    from banana_backup.files import atomic_write
    from banana_backup.git import Git

    token = tmp_path / "token"
    atomic_write(token, "fixture-backup-token")
    work = tmp_path / "client"
    work.mkdir(mode=0o700)
    git = Git(work, {"url": "file://" + str(remote), "forge": "github", "max_mib": 1}, token, allow_local=True)
    largest = []
    run = git.run

    def watched(*arguments, **options):
        result = run(*arguments, **options)
        largest.append(len(result))
        return result

    git.run = watched
    return git, largest


def _bomb(width, depth):
    def entries(git):
        blob = git("hash-object", "-w", "--stdin", data=b"{}")
        tree = git("mktree", data=f"100644 blob {blob}\tx\n".encode())
        for _ in range(depth):
            tree = git("mktree", data="".join(f"040000 tree {tree}\t{i:04d}\n" for i in range(width)).encode())
        return f"100644 blob {blob}\tindex.json\n040000 tree {tree}\tdata\n"
    return entries


def test_rc4_1_a_tree_bomb_is_refused_without_listing_it(tmp_path):
    # 60 * 3 tree entries in the pack, 216,000 lines (about 17 MB) when listed recursively.
    remote, commit = _remote(tmp_path, _bomb(60, 3))
    git, largest = _client(tmp_path, remote)
    with pytest.raises(ValueError):
        git.fetch(REF, commit)
    assert max(largest) <= 128 * 1024 + 1  # nothing near the 17 MB listing was read


def test_rc4_1_a_huge_flat_tree_is_read_only_up_to_the_limit(tmp_path):
    def entries(git):
        blob = git("hash-object", "-w", "--stdin", data=b"{}")
        return "".join(f"100644 blob {blob}\ta{i:06d}\n" for i in range(20_000)) + f"100644 blob {blob}\tindex.json\n"

    remote, commit = _remote(tmp_path, entries)
    git, largest = _client(tmp_path, remote)
    with pytest.raises(ValueError, match="no index"):
        git.index(REF, commit)
    with pytest.raises(ValueError):
        git.fetch(REF, commit)
    assert max(largest) <= 128 * 1024 + 1


def test_rc4_1_too_many_branches_are_refused_unread(tmp_path):
    import subprocess

    remote, commit = _remote(tmp_path, lambda git: f"100644 blob {git('hash-object', '-w', '--stdin', data=b'{}')}\tindex.json\n")
    # About 1.6 MB of ls-remote output, all of it branches whose names end in "/" + REF.
    names = "".join(f"create refs/heads/x{i:05d}-{'p' * 60}/{REF} {commit}\n" for i in range(8000))
    subprocess.run(["git", "--git-dir", str(remote), "update-ref", "--stdin"], input=names.encode(), check=True)
    git, largest = _client(tmp_path, remote)
    with pytest.raises(ValueError, match="Too many backup branches"):
        git.ref_sha(REF)
    with pytest.raises(ValueError, match="Too many backup branches"):
        git.heads("refs/heads/banana-backups/bananachat/main/")
    assert max(largest) <= 1024 * 1024 + 1


def test_rc4_1_a_real_snapshot_tree_is_still_read(tmp_path):
    def entries(git):
        index = git("hash-object", "-w", "--stdin", data=b"{}")
        part = git("hash-object", "-w", "--stdin", data=b"x" * 100)
        return f"100644 blob {index}\tindex.json\n100644 blob {part}\tpart-00000.age\n"

    remote, commit = _remote(tmp_path, entries)
    git, _largest = _client(tmp_path, remote)
    assert git.ref_sha(REF) == commit
    assert set(git.fetch(REF, commit)) == {"index.json", "part-00000.age"}
    assert git.fetch(REF, commit)["part-00000.age"][1] == 100
    assert git.index(REF, commit) == git.fetch(REF, commit)["index.json"][0]
