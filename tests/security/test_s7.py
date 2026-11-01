"""Administrator exports, Markdown chat downloads and backup snapshot branches (full re-audit pass 13, SC):
the database and metrics exports stop once the administrator who started them may no longer read them
(SC1-1); a Markdown chat download holds a bounded number of message bytes while it streams (SC2-1); snapshot
branches pushed by someone else cost a bounded amount of disk and time per backup run (SC4-1)."""
from __future__ import annotations

import gzip
import io
import os
import tarfile
import zlib

import pytest

from tests.app.fixtures import Browser


def _one(app, sql, params=()):
    from bananachat import db
    with app.app_context():
        return db.one(sql, params)


# ----- SC1-1: administrator exports after suspension, demotion or sign-out ------------------------------------

@pytest.fixture
def check_every_piece(monkeypatch):
    from bananachat import security
    monkeypatch.setattr(security, "DOWNLOAD_CHECK_SECONDS", 0, raising=False)


def _second_admin(app, make_user):
    eve = make_user("eve", role="admin")
    browser = Browser(app)
    browser.login("eve")
    return eve, browser


def _start(browser, path, **kwargs):
    response = browser.post(path, buffered=False, **kwargs)
    assert response.status_code == 200
    pieces = iter(response.response)
    return response, pieces, next(pieces)


def _rest(response, pieces):
    try:
        return b"".join(pieces)
    finally:
        response.close()


def _seed_database(app, user_id):
    from bananachat.db import chats
    with app.app_context():
        for _ in range(6):
            session_id = chats.create(user_id)
            chats.add_message(session_id, "user", os.urandom(150_000).hex(), user_id=user_id, incognito=False)


def _database_member_complete(data: bytes) -> bool:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
            member = bundle.getmember("bananachat.db")
            return len(bundle.extractfile(member).read()) == member.size
    except (tarfile.TarError, EOFError, OSError, zlib.error, gzip.BadGzipFile, KeyError):
        return False


def test_sc1_1_database_export_stops_when_the_administrator_is_suspended(app, make_user, admin, check_every_piece):
    eve, browser = _second_admin(app, make_user)
    _seed_database(app, eve["id"])
    response, pieces, first = _start(browser, "/admin/migration/export", data={"reauth_secret": "eve-password"})
    assert admin.post(f"/admin/users/{eve['id']}/suspend", {"until": ""}).status_code == 302
    assert _one(app, "SELECT COUNT(*) AS n FROM auth_sessions WHERE user_id=?", (eve["id"],))["n"] == 0
    assert not _database_member_complete(first + _rest(response, pieces))


def test_sc1_1_database_export_stops_when_the_session_ends(app, make_user, check_every_piece):
    eve, browser = _second_admin(app, make_user)
    _seed_database(app, eve["id"])
    response, pieces, first = _start(browser, "/admin/migration/export", data={"reauth_secret": "eve-password"})
    from bananachat import db
    with app.app_context():
        db.execute("DELETE FROM auth_sessions WHERE user_id=?", (eve["id"],))
    assert not _database_member_complete(first + _rest(response, pieces))


def test_sc1_1_database_export_still_completes(app, make_user, check_every_piece):
    eve, browser = _second_admin(app, make_user)
    _seed_database(app, eve["id"])
    response, pieces, first = _start(browser, "/admin/migration/export", data={"reauth_secret": "eve-password"})
    assert _database_member_complete(first + _rest(response, pieces))


def _seed_metrics(app, user_id, rows=2_000):
    from bananachat.db import metrics
    with app.app_context():
        for number in range(rows):
            metrics.record_request("chat", user_id=user_id, tokens_in=number)


def test_sc1_1_metrics_export_stops_when_the_administrator_is_demoted(app, make_user, admin, check_every_piece):
    eve, browser = _second_admin(app, make_user)
    _seed_metrics(app, eve["id"])
    response, pieces, first = _start(browser, "/admin/metrics/export.csv?range=30d", data={"range": "30d"})
    from bananachat.db import users
    with app.app_context():
        users.set_role(eve["id"], "user")
    lines = (first + _rest(response, pieces)).count(b"\n")
    assert lines < 2_001


def test_sc1_1_metrics_export_stops_when_the_administrator_is_suspended(app, make_user, admin, check_every_piece):
    eve, browser = _second_admin(app, make_user)
    _seed_metrics(app, eve["id"])
    response, pieces, first = _start(browser, "/admin/metrics/export.csv?range=30d", data={"range": "30d"})
    assert admin.post(f"/admin/users/{eve['id']}/suspend", {"until": ""}).status_code == 302
    assert (first + _rest(response, pieces)).count(b"\n") < 2_001


def test_sc1_1_metrics_export_still_completes(app, make_user, check_every_piece):
    eve, browser = _second_admin(app, make_user)
    _seed_metrics(app, eve["id"])
    response, pieces, first = _start(browser, "/admin/metrics/export.csv?range=30d", data={"range": "30d"})
    assert (first + _rest(response, pieces)).count(b"\n") == 2_001


# ----- SC2-1: Markdown chat download memory ------------------------------------------------------------------

MESSAGE_BYTES = 100_000


def _seed_chat(app, make_user, count=20):
    from bananachat.db import chats
    user = make_user("mallory")
    with app.app_context():
        session_id = chats.create(user["id"])
        for number in range(count):
            role = "user" if number % 2 == 0 else "assistant"
            chats.add_message(session_id, role, f"MARK{number:02d}-" + "x" * MESSAGE_BYTES, user_id=user["id"],
                              incognito=False)
        token = chats.share(session_id)
    return user, session_id, token


@pytest.fixture
def batch_sizes(monkeypatch):
    """Record the message text held by each batch the Markdown download reads."""
    from bananachat.db import chats
    from bananachat.web import chat
    monkeypatch.setattr(chat, "PAGE_TEXT_BYTES", 3 * MESSAGE_BYTES)
    original, sizes = chats.iter_message_batches, []

    def recording(*args, **kwargs):
        for rows in original(*args, **kwargs):
            sizes.append(sum(len(row["content"].encode()) for row in rows))
            yield rows

    monkeypatch.setattr(chats, "iter_message_batches", recording)
    return sizes


def test_sc2_1_anonymous_share_download_reads_bounded_batches(app, make_user, batch_sizes):
    _user, _session_id, token = _seed_chat(app, make_user)
    response = app.test_client().get(f"/share/{token}/download")
    assert response.status_code == 200
    data = response.get_data(as_text=True)
    positions = [data.index(f"MARK{number:02d}-") for number in range(20)]
    assert positions == sorted(positions)
    assert batch_sizes and max(batch_sizes) <= 3 * (MESSAGE_BYTES + 16)


def test_sc2_1_owner_download_reads_bounded_batches(app, make_user, batch_sizes):
    _user, session_id, _token = _seed_chat(app, make_user)
    browser = Browser(app)
    browser.login("mallory")
    response = browser.get(f"/chat/{session_id}/download")
    assert response.status_code == 200
    assert all(f"MARK{number:02d}-" in response.get_data(as_text=True) for number in range(20))
    assert max(batch_sizes) <= 3 * (MESSAGE_BYTES + 16)


def test_sc2_1_message_batches_hold_at_least_one_message(app, make_user):
    from bananachat.db import chats
    _user, session_id, _token = _seed_chat(app, make_user, count=5)
    with app.app_context():
        batches = list(chats.iter_message_batches(session_id, max_bytes=10))
        whole = list(chats.iter_message_batches(session_id))
    assert [len(rows) for rows in batches] == [1] * 5
    assert [row["id"] for rows in batches for row in rows] == [row["id"] for row in whole[0]]


# ----- SC4-1: snapshot branches pushed by someone else -------------------------------------------------------

SERIES_PREFIX = "refs/heads/banana-backups/bananachat/main/"


def _branches_remote(tmp_path, count, blobs=200):
    """A bare remote with *count* snapshot branches dated now, each {index.json, payload/ -> one shared subtree}."""
    import subprocess
    from datetime import datetime, timedelta, timezone

    remote = tmp_path / "remote.git"
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@y", "GIT_COMMITTER_NAME": "x",
                   "GIT_COMMITTER_EMAIL": "x@y"}

    def git(*arguments, data=None):
        return subprocess.run(["git", "--git-dir", str(remote), *arguments], input=data, env=environment, check=True,
                              capture_output=True).stdout.decode().strip()

    subprocess.run(["git", "init", "-q", "--bare", str(remote)], env=environment, check=True)
    git("config", "uploadpack.allowFilter", "true")
    stream = b"".join(b"blob\nmark :%d\ndata 300\n%s\n" % (number + 1, os.urandom(150).hex().encode())
                      for number in range(blobs))
    stream += (b"commit refs/heads/s\ncommitter x <x@y> 0 +0000\ndata 0\n"
               + b"".join(b"M 100644 :%d d/%06d\n" % (number + 1, number) for number in range(blobs)) + b"\n")
    git("fast-import", "--quiet", data=stream)
    subtree = git("rev-parse", "refs/heads/s:d")
    git("update-ref", "-d", "refs/heads/s")
    now, updates, names = datetime.now(timezone.utc), "", []
    for number in range(count):
        blob = git("hash-object", "-w", "--stdin", data=b'{"x": %d}' % number)
        tree = git("mktree", data=f"100644 blob {blob}\tindex.json\n040000 tree {subtree}\tpayload\n".encode())
        commit = git("commit-tree", tree, "-m", "m")
        name = (now - timedelta(minutes=number)).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{number:08x}"
        names.append(name)
        updates += f"create {SERIES_PREFIX}{name} {commit}\n"
    git("update-ref", "--stdin", data=updates.encode())
    return remote, names


def _backup_git(tmp_path, remote):
    from banana_backup.files import atomic_write
    from banana_backup.git import Git

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(mode=0o700)
    atomic_write(secrets_dir / "token", "fixture-backup-token")
    work = tmp_path / "client"
    work.mkdir(mode=0o700)
    config = {"url": "file://" + str(remote), "forge": "github", "max_mib": 512, "name": "main", "keep": 7}
    return Git(work, config, secrets_dir / "token", allow_local=True), config


def test_sc4_1_snapshot_indexes_do_not_pile_up_fetched_objects(tmp_path, monkeypatch):
    from banana_backup.git import Git

    remote, _names = _branches_remote(tmp_path, 6)
    git, _config = _backup_git(tmp_path, remote)
    calls = []
    original = Git.run

    def counting(self, *arguments, **kwargs):
        calls.append(arguments[0])
        return original(self, *arguments, **kwargs)

    monkeypatch.setattr(Git, "run", counting)
    heads = git.heads(SERIES_PREFIX)
    for name, sha in heads.items():
        before = calls.count("show-index")
        blob = git.index(SERIES_PREFIX + name, sha)
        assert calls.count("show-index") - before <= 1
        assert git.run("cat-file", "blob", blob, bounded=True).startswith(b'{"x": ')
        # Nothing of the snapshot's subtree stays behind: no pack, no scratch repository.
        assert not list((git.repository / "objects" / "pack").glob("*.pack"))
        assert sorted(path.name for path in git.work.iterdir()) == ["askpass", "git"]


def _cap():
    from banana_backup import store as store_module
    return getattr(store_module, "MAX_UNVERIFIED", 40)


def _store(tmp_path):
    from banana_backup.store import Store
    return Store(tmp_path / "store", "BananaChat")


def test_sc4_1_retention_reads_a_bounded_number_of_unauthenticated_branches(tmp_path, monkeypatch):
    from banana_backup import store as store_module

    store = _store(tmp_path)
    read = []
    monkeypatch.setattr(store_module.Store, "_authenticated_index",
                        lambda self, git, config, name, sha: read.append(name))
    remote, names = _branches_remote(tmp_path, _cap() + 25, blobs=1)
    git, config = _backup_git(tmp_path, remote)
    removed, warnings = store._prune(git, config, git.heads(SERIES_PREFIX), 6)
    assert removed == [] and len(read) == _cap()
    assert read == names[:_cap()]
    assert any("not read" in warning for warning in warnings)


def test_sc4_1_series_start_is_refused_behind_too_many_unauthenticated_branches(tmp_path, monkeypatch):
    from banana_backup import store as store_module

    store = _store(tmp_path)
    read = []
    monkeypatch.setattr(store_module.Store, "_authenticated_index",
                        lambda self, git, config, name, sha: read.append(name))
    heads = {name: "0" * 40 for name in _names_now(_cap() + 25)}
    with pytest.raises(ValueError, match="not authenticated"):
        store._authenticated_since(None, {"name": "main"}, heads)
    assert len(read) == _cap() + 1


def test_sc4_1_genuine_series_behind_a_few_unauthenticated_branches_still_work(tmp_path, monkeypatch):
    from banana_backup import store as store_module

    store = _store(tmp_path)
    names = _names_now(_cap() + 10)
    genuine = set(names[5:])
    monkeypatch.setattr(store_module.Store, "_authenticated_index",
                        lambda self, git, config, name, sha: {"authenticated_since": names[-1]} if name in genuine
                        else None)
    heads = {name: "0" * 40 for name in names}
    assert store._authenticated_since(None, {"name": "main"}, heads) == names[-1]
    deleted = []

    class FakeGit:
        def remove(self, ref, sha):
            deleted.append(ref.rsplit("/", 1)[1])

    removed, _warnings = store._prune(FakeGit(), {"name": "main"}, heads, 6)
    assert removed == deleted == names[11:]


def _names_now(count):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return [(now - timedelta(minutes=number)).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{number:08x}"
            for number in range(count)]
