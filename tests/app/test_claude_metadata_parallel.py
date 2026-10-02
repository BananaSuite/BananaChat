"""Many private profiles cannot turn one metadata refresh into unbounded work."""
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from bananachat.db import claude_pool as accounts
from bananachat.services import claude_code
from bananachat.services.claude_code import Adapter
from bananachat.services.upstream import Cancelled, CancelToken

MODEL = {"name": "parallel-sonnet", "family": "sonnet", "reasoning": ["low"]}
FAKE = '''#!/usr/bin/env python3
import json,os,signal,sys,time
from pathlib import Path
home=Path(os.environ['HOME'])
if (home/'hang').exists():
 (home/'pid').write_text(str(os.getpid()))
 if (home/'child').exists():
  child=os.fork()
  if child==0:
   signal.signal(signal.SIGTERM,signal.SIG_IGN)
   time.sleep(60)
   sys.exit(0)
  (home/'child-pid').write_text(str(child))
 time.sleep(60)
print(json.dumps({'loggedIn':not (home/'loggedout').exists(),
                  'authMethod':'api_key' if (home/'apikey').exists() else 'claude.ai',
                  'email':home.name+'@parallel.test'}),flush=True)
'''


@pytest.fixture
def metadata_adapter(tmp_path, monkeypatch):
    def build(count=8, *, fake=False):
        binary = tmp_path / "claude"
        binary.write_text(FAKE)
        binary.chmod(0o700)
        manifest = {"binary": str(binary) if fake else sys.executable, "profiles": {}}
        homes = {}
        for index in range(count):
            name = f"profile-{index}"
            home = tmp_path / name
            config = home / "config"
            config.mkdir(parents=True, exist_ok=True)
            home.chmod(0o700)
            config.chmod(0o700)
            manifest["profiles"][name] = {"home": str(home), "config_dir": str(config), "models": [MODEL],
                                           "usage_source": "local"}
            homes[name] = home
        path = tmp_path / "connector.json"
        path.write_text(json.dumps(manifest))
        path.chmod(0o600)
        adapter = Adapter(SimpleNamespace(claude_code_config=str(path)))

        def bindings():
            assert not threading.current_thread().name.startswith("bc-claude-metadata")
            return {str(index + 1): name for index, name in enumerate(homes)}

        def list_accounts(**_):
            assert not threading.current_thread().name.startswith("bc-claude-metadata")
            return [{"id": index + 1, "label": name} for index, name in enumerate(homes)]

        adapter.bindings = bindings
        monkeypatch.setattr(accounts, "list_accounts", list_accounts)
        return adapter, homes

    return build


def test_concurrent_explicit_refreshes_share_one_four_worker_batch(metadata_adapter):
    adapter, _ = metadata_adapter()
    active, peak, calls = 0, 0, 0
    lock = threading.Lock()
    barrier = threading.Barrier(6)

    def worker(kind, name, profile, force, cancel):
        nonlocal active, peak, calls
        with lock:
            active += 1
            peak = max(peak, active)
            calls += 1
        try:
            assert cancel.wait(0.08) is False
            return name, {"source": "local", "available": True}
        finally:
            with lock:
                active -= 1

    adapter._metadata_profile = worker

    def refresh():
        barrier.wait()
        return adapter.usage_reports(force=True)

    with ThreadPoolExecutor(max_workers=6) as executor:
        reports = list(executor.map(lambda _: refresh(), range(6)))
    assert all(len(report) == 8 and all(value["available"] for value in report.values()) for report in reports)
    assert calls == 8 and peak == 4 and active == 0
    adapter.usage_reports()
    assert calls == 8


def test_deadline_cancels_queued_profiles_and_retains_healthy_result(metadata_adapter, monkeypatch):
    adapter, _ = metadata_adapter(40)
    monkeypatch.setattr(claude_code, "METADATA_SECONDS", 0.25)
    called, finished = [], []
    lock = threading.Lock()

    def worker(kind, name, profile, force, cancel):
        with lock:
            called.append(name)
        try:
            if name == "profile-0":
                return name, {"source": "local", "available": True}
            cancel.wait(5)
            cancel.check()
        finally:
            with lock:
                finished.append(name)

    adapter._metadata_profile = worker
    started = time.monotonic()
    reports = adapter.usage_reports()
    assert time.monotonic() - started < 1
    assert reports["1"]["available"] is True
    assert all(report["available"] is False for aid, report in reports.items() if aid != "1")
    assert len(called) <= 5 and sorted(finished) == sorted(called)
    adapter.usage_reports()
    assert len(called) <= 5  # Failure snapshots also suppress immediate retries.


@pytest.mark.parametrize("external_cancel", [False, True])
def test_three_real_hung_profiles_stop_with_children_before_return(metadata_adapter, monkeypatch, external_cancel):
    adapter, homes = metadata_adapter(4, fake=True)
    for name in ("profile-0", "profile-1", "profile-2"):
        (homes[name] / "hang").touch()
        (homes[name] / "child").touch()
    monkeypatch.setattr(claude_code, "METADATA_SECONDS", 0.5)
    cancel = CancelToken()
    timer = threading.Timer(0.25, cancel.cancel) if external_cancel else None
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        if external_cancel:
            with pytest.raises(Cancelled):
                adapter.discover(cancel=cancel)
        else:
            models = adapter.discover()
            assert len(models) == 1 and models[0]["account_ids"] == [4]
    finally:
        if timer:
            timer.cancel()
    assert time.monotonic() - started < 8
    for name in ("profile-0", "profile-1", "profile-2"):
        pid = int((homes[name] / "pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        child = int((homes[name] / "child-pid").read_text())
        status = Path(f"/proc/{child}/stat")
        assert not status.exists() or status.read_text().rsplit(")", 1)[1].split()[0] in ("Z", "X")
    assert not any(thread.name.startswith("bc-claude-metadata") for thread in threading.enumerate())


def test_profile_model_invalidation_also_clears_coalesced_discovery_snapshot(metadata_adapter):
    adapter, _ = metadata_adapter(1)
    calls = []

    def worker(kind, name, profile, force, cancel):
        calls.append(name)
        return name, [MODEL] if len(calls) == 1 else []

    adapter._metadata_profile = worker
    assert adapter.discover()
    assert adapter.discover() and len(calls) == 1
    adapter.invalidate_models()
    assert adapter.discover() == [] and len(calls) == 2


def test_identity_timeout_does_not_admit_uncertain_subscription(metadata_adapter, monkeypatch):
    adapter, _ = metadata_adapter(8)
    monkeypatch.setattr(claude_code, "METADATA_SECONDS", 0.15)
    adapter._identities["profile-0"] = (time.monotonic(), "first-identity")
    seen = []

    def worker(kind, name, profile, force, cancel):
        assert kind == "identity"
        seen.append(name)
        cancel.wait(5)
        cancel.check()

    adapter._metadata_profile = worker
    from bananachat.services.claude_pool import AdmissionChanged
    with pytest.raises(AdmissionChanged):
        adapter._require_unique_identity("profile-0")
    assert len(seen) == 4


def test_completed_auth_timeout_is_not_treated_as_confirmed_logout(metadata_adapter):
    from bananachat.services.claude_pool import AdmissionChanged

    adapter, homes = metadata_adapter(2, fake=True)
    adapter.check_profile("profile-0")
    (homes["profile-1"] / "hang").touch()
    original = adapter._records

    def short_auth_timeout(profile, arguments, *args, **kwargs):
        if arguments == ["auth", "status"]:
            kwargs["timeout"] = 0.15
        return original(profile, arguments, *args, **kwargs)

    adapter._records = short_auth_timeout
    with pytest.raises(AdmissionChanged):
        adapter._require_unique_identity("profile-0")
    cached = adapter._metadata_cache["identity"][2]
    assert "profile-1" in cached and cached["profile-1"] is None


@pytest.mark.parametrize("flag", ["loggedout", "apikey"])
def test_confirmed_logged_out_or_api_key_profile_does_not_disable_healthy_account(metadata_adapter, flag):
    adapter, homes = metadata_adapter(2, fake=True)
    adapter.check_profile("profile-0")
    (homes["profile-1"] / flag).touch()
    adapter._require_unique_identity("profile-0")
    cached = adapter._metadata_cache["identity"][2]
    assert cached["profile-1"] == claude_code.AUTHENTICATION_FAILED


def test_process_cleanup_failure_still_closes_all_pipes(metadata_adapter, monkeypatch):
    adapter, _ = metadata_adapter(1, fake=True)
    started = []
    original_popen = claude_code.subprocess.Popen
    original_stop = claude_code._stop

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        started.append(process)
        return process

    def failed_stop(process):
        original_stop(process)
        raise claude_code.ProcessCleanupError("Synthetic cleanup failure.")

    monkeypatch.setattr(claude_code.subprocess, "Popen", popen)
    monkeypatch.setattr(claude_code, "_stop", failed_stop)
    with pytest.raises(claude_code.ProcessCleanupError):
        list(adapter._records(adapter.profiles["profile-0"], ["auth", "status"], single_json=True))
    assert len(started) == 1
    assert all(pipe.closed for pipe in (started[0].stdin, started[0].stdout, started[0].stderr))


def test_group_stat_parser_handles_parentheses_and_zombies(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    child = root / "123"
    child.mkdir()
    stat = child / "stat"
    stat.write_text("123 (name with ) spaces) R 1 456 456 0 0\n")
    original_scandir = os.scandir
    monkeypatch.setattr(claude_code.os, "scandir", lambda _: original_scandir(root))
    assert claude_code._group_running(456) is True
    stat.write_text("123 (name with ) spaces) Z 1 456 456 0 0\n")
    assert claude_code._group_running(456) is False


def test_invalidation_during_discovery_does_not_repopulate_old_profile_cache(metadata_adapter, monkeypatch):
    from bananachat.services import claude_discovery

    adapter, _ = metadata_adapter(1, fake=True)
    profile = adapter.profiles["profile-0"]
    profile.update(discovery="automatic", models=[])
    entered, finish = threading.Event(), threading.Event()

    def discover(*args, **kwargs):
        entered.set()
        assert finish.wait(3)
        return [MODEL]

    monkeypatch.setattr(claude_discovery, "discover", discover)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(adapter._profile_models, "profile-0", profile)
        assert entered.wait(3)
        adapter.invalidate_models()
        finish.set()
        assert result.result() == [MODEL]
    assert "profile-0" not in adapter._models
