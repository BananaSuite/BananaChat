#!/usr/bin/env python3
"""Measure HTTP and admission behaviour of a local Gunicorn deployment.

Starts ``gunicorn -c gunicorn.conf.py wsgi:app`` on a temporary instance with
an imitation Ollama server (fixed per-token delay), then runs:

1. page reads (the chat page with a long conversation) at 1, 8 and 16 clients;
2. API chat completions at 8 clients;
3. a burst of 64 simultaneous completions from 64 accounts (bounded queue);
4. a burst of 16 simultaneous completions from one account (per-account limit);
5. health probes during the bursts.

It measures the web tier only: no GPU or real model is involved.
Usage: ``python scripts/load_test.py`` (prints a Markdown table).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url, *, data=None, headers=None, timeout=120):
    started = time.perf_counter()
    try:
        with OPENER.open(urllib.request.Request(url, data=data, headers=headers or {}), timeout=timeout) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        body = error.read()
        status = error.code
        try:
            status = f"{error.code} {json.loads(body)['error']['code']}"
        except (ValueError, KeyError, TypeError):
            pass
    return status, time.perf_counter() - started


def prepare(instance: Path, ollama_url: str):
    """Create users, a long chat and an API token with the application code itself."""
    from bananachat import create_app, db, security
    from bananachat.config import load_config
    from bananachat.db import catalog, settings, tokens, users
    from bananachat.services import ollama

    config = load_config({"BC_INSTANCE_DIR": str(instance), "BC_OLLAMA_URL": ollama_url,
                          "BC_SECRET_KEY": "load-test-secret-key-0123456789abcdef0123"})
    app = create_app(config)
    with app.test_request_context():
        with db.transaction():
            admin = users.create("admin", security.hash_password("admin-password"), role="admin")
            user = users.create("reader", security.hash_password("reader-password"))
            settings.update(setup_done=1, api_rpm=0, default_daily_credits=1_000_000)
        ollama.sync_catalog()
        for model in catalog.list_models():
            catalog.set_rollout(model["id"], True)
        raw = []
        for index in range(64):
            owner = user if index == 0 else users.create(f"client{index}", security.hash_password("client-password"))
            raw.append(tokens.create(owner, "load")[1])
        session_id = "loadtestsession0000000"
        db.execute("INSERT INTO chat_sessions (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                   (session_id, user, "Long chat", db.now(), db.now()))
        with db.transaction():
            for index in range(25000):
                db.execute("INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                           (session_id, "user" if index % 2 == 0 else "assistant", "x" * 500, db.now()))
    db.close_thread_connection()
    return raw, session_id, admin


def login_cookie(base: str) -> str:
    """Sign in through the real form and return the session cookie header."""
    import http.cookiejar
    import re

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))
    page = opener.open(base + "/login").read().decode()
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    form_time = re.search(r'name="_form_time" value="([^"]+)"', page).group(1)
    time.sleep(0.5)
    body = urllib.parse.urlencode({"csrf_token": csrf, "_form_time": form_time, "username": "reader",
                                   "password": "reader-password"}).encode()
    opener.open(base + "/login", data=body)
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in jar)


def summarise(label, results, elapsed):
    statuses = {}
    for status, _ in results:
        status = str(status)
        statuses[status] = statuses.get(status, 0) + 1
    latencies = sorted(duration for status, duration in results if status in (200, "200"))
    p95 = latencies[int(len(latencies) * 0.95) - 1] * 1000 if latencies else float("nan")
    throughput = f"{len(results) / elapsed:.1f}/s" if elapsed else "–"
    outcome = ", ".join(f"{count}× HTTP {status}" for status, count in sorted(statuses.items()))
    return f"| {label} | {len(results)} | {throughput} | {p95:.0f} ms | {outcome} |"


def run() -> int:
    from tests.app.fake_ollama import FakeOllama

    fake = FakeOllama().start()
    fake.reply = " ".join(["token"] * 60)
    fake.chunk_delay = 0.01
    instance = Path(tempfile.mkdtemp(prefix="bananachat-load-"))
    raw_tokens, session_id, _ = prepare(instance, fake.url)
    port = free_port()
    environment = {**os.environ, "BC_INSTANCE_DIR": str(instance), "BC_OLLAMA_URL": fake.url, "BC_PORT": str(port),
                   "BC_SECRET_KEY": "load-test-secret-key-0123456789abcdef0123", "BC_LOGGING_LEVEL": "minimal",
                   "BC_MIN_FREE_MEMORY_MB": "0"}
    log = open(instance / "gunicorn.log", "w")
    server = subprocess.Popen([sys.executable, "-m", "gunicorn", "-c", "gunicorn.conf.py", "wsgi:app",
                               "--access-logfile", "/dev/null"], cwd=ROOT, env=environment, stdout=log,
                              stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    rows = []
    memory = None
    try:
        deadline = time.monotonic() + 60
        while not _up(base):
            if time.monotonic() > deadline or server.poll() is not None:
                raise SystemExit("Gunicorn did not start; see " + str(instance / "gunicorn.log"))
            time.sleep(0.2)
        cookie = login_cookie(base)
        page = f"{base}/chat/{session_id}"

        for clients in (1, 8, 16):
            count = 96 if clients > 1 else 24
            started = time.perf_counter()
            with ThreadPoolExecutor(clients) as pool:
                results = list(pool.map(lambda _: request(page, headers={"Cookie": cookie}), range(count)))
            rows.append(summarise(f"Chat page with 25,000 messages, {clients} client(s)", results,
                                  time.perf_counter() - started))

        body = json.dumps({"model": "auto", "messages": [{"role": "user", "content": "Hello"}]}).encode()

        def complete(token):
            return request(base + "/v1/chat/completions", data=body,
                           headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

        started = time.perf_counter()
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(complete, [raw_tokens[index % 8] for index in range(48)]))
        rows.append(summarise("API completions (60 streamed tokens each), 8 accounts", results,
                              time.perf_counter() - started))

        health = []
        stop = threading.Event()

        def probe():
            while not stop.is_set():
                started = time.perf_counter()
                try:
                    health.append(request(base + "/health", timeout=5))
                except OSError:
                    health.append(("timeout", time.perf_counter() - started))
                time.sleep(0.05)

        prober = threading.Thread(target=probe)
        prober.start()
        started = time.perf_counter()
        with ThreadPoolExecutor(64) as pool:
            results = list(pool.map(complete, raw_tokens))
        rows.append(summarise("Burst: 64 simultaneous completions, 64 accounts", results,
                              time.perf_counter() - started))
        started = time.perf_counter()
        with ThreadPoolExecutor(16) as pool:
            results = list(pool.map(complete, [raw_tokens[0]] * 16))
        rows.append(summarise("Burst: 16 simultaneous completions, one account", results,
                              time.perf_counter() - started))
        stop.set()
        prober.join()
        rows.append(summarise("Health probes (5 s timeout) during the bursts", health, 0))
        try:
            import psutil

            processes = [psutil.Process(server.pid), *psutil.Process(server.pid).children(recursive=True)]
            memory = sum(process.memory_info().rss for process in processes) / 2**20
        except (ImportError, OSError):
            memory = None
    finally:
        server.terminate()
        server.wait(timeout=60)
        fake.stop()
        log.close()
    print("| Workload | Requests | Throughput | 95th percentile | Result |")
    print("| --- | ---: | ---: | ---: | --- |")
    print("\n".join(rows))
    with sqlite3.connect(instance / "bananachat.db") as conn:
        charged = conn.execute("SELECT COUNT(*) FROM credit_ledger WHERE request_type='api'").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"\nLedger rows for API requests: {charged}; database integrity: {integrity}")
    if memory is not None:
        print(f"Resident memory of the Gunicorn processes after the run: {memory:.0f} MiB")
    return 0


def _up(base):
    try:
        return request(base + "/health", timeout=2)[0] == 200
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(run())
