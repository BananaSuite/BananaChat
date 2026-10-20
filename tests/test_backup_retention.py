"""Remote backup retention and restores trust only what the series' recovery key authenticated.

Snapshot IDs are branch names chosen by whoever can push to the backup
repository (the threat an append-only series exists for):

* retention counts only authenticated snapshots, newest first, never deletes
  while fewer than ``keep`` of them would remain, and neither counts nor
  deletes IDs dated in the future or that are no dates;
* no ID, however crafted, keeps uploads from working;
* ``--allow-unauthenticated`` never restores a snapshot dated at or after
  the series' first authenticated one (which every authenticated index
  records, so retention deleting it changes nothing), or dated in the
  future.
"""

from datetime import datetime, timedelta, timezone
import json
import subprocess

import pytest

from banana_backup.files import write_json
from test_git_backups import command, forge, key, package, remote, store  # noqa: F401


def stamp(delta=timedelta(0)):
    return (datetime.now(timezone.utc) + delta).strftime("%Y%m%dT%H%M%S.%fZ")


def garbage(store, identifier):
    """A branch named like a snapshot, holding anything: no key needed."""
    config = store.settings()
    with store.transport(config) as git:
        write_json(git.work / "index.json", {"garbage": True})
        git.push(git.commit([git.work / "index.json"]), store.prefix(config) + identifier)
    return identifier


def append_only(store, keep):
    path = store.root / "repository.json"
    write_json(path, {**json.loads(path.read_text()), "append_only": True, "keep": keep})


def upload(store, tmp_path, count):
    return [store.upload(package(tmp_path, b"genuine %d" % number))["snapshot"] for number in range(count)]


def listed(store):
    return {item["snapshot"] for item in store.list()}


def test_branches_dated_in_the_future_never_make_prune_delete_genuine_snapshots(store, tmp_path):
    append_only(store, 3)
    genuine = upload(store, tmp_path, 3)
    bogus = [garbage(store, f"99991231T23595{number}.000000Z-0000000{number}") for number in range(3)]
    result = store.prune()
    assert result["removed"] == []
    assert listed(store) == {*genuine, *bogus}
    assert any(all(name in warning for name in bogus) for warning in result["warnings"])


def test_unauthenticated_snapshots_do_not_count_toward_keep(store, tmp_path):
    append_only(store, 2)
    genuine = upload(store, tmp_path, 3)
    older = forge(store, tmp_path, identifier=stamp(-timedelta(days=30)) + "-0dd0dd00")
    newer = [garbage(store, stamp(timedelta(minutes=number)) + f"-f0f0f0f{number}") for number in (1, 2, 3)]
    result = store.prune()
    assert sorted(result["removed"]) == sorted([older, genuine[0]])
    assert listed(store) == {*genuine[1:], *newer}
    assert any(all(name in warning for name in newer) for warning in result["warnings"])


def test_pruning_after_an_upload_counts_only_authenticated_snapshots_too(store, tmp_path):
    genuine = upload(store, tmp_path, 2)  # keep=2, not append-only: each upload prunes
    forged = [garbage(store, stamp(timedelta(minutes=number)) + f"-0000000{number}") for number in (1, 2)]
    latest = upload(store, tmp_path, 1)
    assert listed(store) == {genuine[1], *latest, *forged}


@pytest.mark.parametrize("identifier", ["99991231T235959.999999Z-ffffffff", "99999999T999999Z-00000000",
                                        "20261301T000000.000000Z-abcdef01"])
def test_no_crafted_snapshot_id_breaks_uploads(store, tmp_path, identifier):
    upload(store, tmp_path, 1)
    garbage(store, identifier)
    uploaded = upload(store, tmp_path, 1)[0]
    assert uploaded in listed(store) and uploaded < "9"
    assert store.download(uploaded, tmp_path / "restored.tar.gz")["authenticated"] is True


def test_an_unauthenticated_snapshot_newer_than_an_authenticated_one_is_never_restored(store, tmp_path):
    upload(store, tmp_path, 1)
    pre_authentication = forge(store, tmp_path, identifier=stamp(-timedelta(days=30)) + "-0dd0dd00")
    forged = forge(store, tmp_path, identifier=stamp(timedelta(minutes=1)) + "-feedf00d")
    with pytest.raises(ValueError, match="authenticated since"):
        store.download(forged, tmp_path / "forged.tar.gz", allow_unauthenticated=True)
    assert not (tmp_path / "forged.tar.gz").exists()
    future = forge(store, tmp_path, identifier="99991231T235959.000000Z-feedf00d")
    with pytest.raises(ValueError, match="dated in the future"):
        store.download(future, tmp_path / "future.tar.gz", allow_unauthenticated=True)
    result = store.download(pre_authentication, tmp_path / "old.tar.gz", allow_unauthenticated=True)
    assert result["authenticated"] is False
    with pytest.raises(ValueError, match="--allow-unauthenticated") as error:
        store.download(pre_authentication, tmp_path / "again.tar.gz")
    assert "it was made before" not in str(error.value)


def test_a_snapshot_uploaded_after_going_back_to_an_earlier_release_is_refused_but_recoverable_by_hand(
        store, remote, tmp_path):
    """1.6 uploads unauthenticated snapshots: after a rollback to it, they are newer than authenticated ones."""
    upload(store, tmp_path, 1)
    identifier = forge(store, tmp_path, identifier=stamp(timedelta(seconds=1)) + "-16161616")
    with pytest.raises(ValueError, match="went back to it") as error:
        store.download(identifier, tmp_path / "refused.tar.gz", allow_unauthenticated=True)
    assert "by hand" in str(error.value)
    # As the backup documentation does it: the parts in order, decrypted, without the two header lines.
    ref = store.prefix(store.settings()) + identifier
    parts = sorted(name for name in command(remote, "ls-tree", "--name-only", ref).decode().split()
                   if name.startswith("part-"))
    ciphertext = b"".join(command(remote, "show", ref + ":" + name) for name in parts)
    wrapped = subprocess.run(["age", "--decrypt", "--identity", str(store.identity)], input=ciphertext,
                             capture_output=True, check=True).stdout
    recovered = subprocess.run(["tail", "-n", "+3"], input=wrapped, capture_output=True, check=True).stdout
    assert recovered == b"a package whose source.tar.gz and app.env a restore would deploy as root"


def _between(first, second):
    """A snapshot ID dated between two others."""
    times = [datetime.strptime(name.split("Z-")[0], "%Y%m%dT%H%M%S.%f").replace(tzinfo=timezone.utc)
             for name in (first, second)]
    middle = times[0] + (times[1] - times[0]) / 2
    assert times[0] < middle < times[1]
    return middle.strftime("%Y%m%dT%H%M%S.%fZ") + "-feedf00d"


def test_an_unauthenticated_snapshot_dated_like_the_first_authenticated_one_is_never_restored(store, tmp_path):
    # A series' IDs only ever grow, so an unauthenticated ID equal to an authenticated
    # one is refused as well as a later one.
    (first,) = upload(store, tmp_path, 1)
    same = forge(store, tmp_path, identifier=first.split("Z-")[0] + "Z-00000000")
    with pytest.raises(ValueError, match="authenticated since"):
        store.download(same, tmp_path / "same.tar.gz", allow_unauthenticated=True)
    assert not (tmp_path / "same.tar.gz").exists()


def test_retention_does_not_forget_when_authentication_began(store, tmp_path):
    # The boundary was the oldest authenticated snapshot still in the repository; retention
    # deletes old ones, so a forgery dated after authentication began passed once they were gone.
    genuine = upload(store, tmp_path, 3)  # keep=2, not append-only: each upload prunes
    assert genuine[0] not in listed(store)
    forged = forge(store, tmp_path, identifier=_between(genuine[0], genuine[1]))
    with pytest.raises(ValueError, match=f"authenticated since {genuine[0]}"):
        store.download(forged, tmp_path / "forged.tar.gz", allow_unauthenticated=True)
    older = forge(store, tmp_path, identifier=stamp(-timedelta(days=1)) + "-0dd0dd00")
    assert store.download(older, tmp_path / "old.tar.gz", allow_unauthenticated=True)["authenticated"] is False
