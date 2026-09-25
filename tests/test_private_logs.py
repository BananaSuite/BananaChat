"""Protect private diagnostics against process races, flooding and links."""

import multiprocessing
import os

import pytest

import private_logs


def _write_records(path, prefix):
    for index in range(40):
        private_logs.append(path, f"{prefix}:{index}")


def test_processes_keep_complete_private_records(tmp_path):
    path = tmp_path / "logs" / "errors.log"
    processes = [multiprocessing.get_context("spawn").Process(target=_write_records, args=(path, index)) for index in range(4)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert set(path.read_text().splitlines()) == {f"{worker}:{index}" for worker in range(4) for index in range(40)}
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)


def test_log_flood_has_bounded_retention_and_tail(tmp_path):
    path = tmp_path / "logs" / "errors.log"
    for index in range(50):
        private_logs.append(path, f"{index}:" + "x" * 1000, max_bytes=2000, backups=2)
    files = [file for file in path.parent.iterdir() if not file.name.endswith(".lock")]
    assert len(files) == 3 and sum(file.stat().st_size for file in files) < 6000
    assert private_logs.tail(path, 10) == "...\n" + path.read_text()[-10:]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and hardlink protections")
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_log_paths_never_follow_links_or_block(tmp_path, kind):
    target, path = tmp_path / "private-file", tmp_path / "errors.log"
    target.write_text("preserve")
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        os.mkfifo(path)
    with pytest.raises(OSError):
        private_logs.append(path, "must not be appended")
    with pytest.raises(OSError):
        private_logs.tail(path)
    assert target.read_text() == "preserve"
