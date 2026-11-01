"""Maintenance access to worker PCs: SSH, VNC, RDP and a built-in terminal, through tunnels the workers open.

Worker PCs connect out to the server and need no open port. When the server
allows it (``BC_WORKER_REMOTE_ACCESS``) **and** the worker's owner allows it
(``BC_WORKER_REMOTE_ACCESS=1`` in the worker's settings), an administrator can
open a session to a service on the worker: its own SSH, VNC or RDP server, a
terminal the daemon starts itself (only when the owner also set
``BC_WORKER_REMOTE_SHELL=1``), or, through the worker, a machine of the owner's
network that the owner listed (``BC_WORKER_REMOTE_SUBNETS``). The worker checks
every request against its owner's settings again; the server never decides for
it.

A session is a row in ``worker_tunnels``. The worker learns about it from a
long poll (``/worker/v1/remote/poll``) that it runs while it is **armed**
(an administrator opened its remote panel or a session in the last
:data:`ARM_SECONDS`; it hears about that in its next heartbeat), then opens a
WebSocket for the session; the administrator's browser or the connect helper
opens the other one, and ``services.tunnel_broker`` joins them.

Each worker has an address in ``BC_WORKER_SUBNET`` (a *virtual* network, never
routed by any operating system): the connect helper (``bananachat_connect.py``)
uses it, so ``ssh user@10.77.0.2`` reaches the second worker's SSH server from
an administrator's computer, whatever network the worker is on.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import re
import secrets
import threading
import time
import uuid

from flask import current_app

from bananachat import db
from bananachat.db import worker_remote as store
from bananachat.db import workers as worker_store
from bananachat.services import background

log = logging.getLogger("bananachat.worker_remote")

ARM_SECONDS = 15 * 60
CLAIM_SECONDS = 45             # a session the worker did not pick up by then fails
CONNECT_SECONDS = 90           # and one it picked up but did not connect
LONG_POLL_SECONDS = 20
MAX_REMOTE_POLLS = 2           # waiting remote polls per process (each holds an HTTP thread)
KEY_PREFIX = "bca_"            # noqa: S105 - the public prefix of access keys
KEY_HOURS = 8
KEY_MAX_HOURS = 24 * 30
KEEP_TUNNELS_SECONDS = 7 * 86400
MAX_SERVICES = 16
MAX_SUBNETS = 16
KINDS = ("shell", "ssh", "vnc", "rdp", "tcp")
DEFAULT_PORTS = {"ssh": 22, "vnc": 5900, "rdp": 3389}
LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")
_NAME_RE = re.compile(r"[^a-z0-9]+")


class RemoteError(ValueError):
    """Shown to the administrator (or the connect helper) as it is."""


def _config(config=None):
    return config or current_app.config["BC"]


def enabled(config=None) -> bool:
    config = _config(config)
    return bool(config.workers_enabled and config.worker_remote_access)


# ----- what a worker advertises ---------------------------------------------------------------------------------

def _port(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("invalid port")
    return value


def parse_remote(value) -> dict | None:
    """Validate the ``remote`` object of a heartbeat (what the owner allows); None when absent."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid remote access settings.")
    enabled_flag = value.get("enabled") is True
    for key in ("services", "subnets", "subnet_ports"):
        if value.get(key) is not None and not isinstance(value.get(key), list):
            raise ValueError("Invalid remote access settings.")
    services = []
    for item in (value.get("services") or [])[:MAX_SERVICES]:
        if not isinstance(item, dict) or item.get("kind") not in ("ssh", "vnc", "rdp", "tcp"):
            raise ValueError("Invalid remote service.")
        services.append({"kind": item["kind"], "port": _port(item.get("port")),
                         "listening": item.get("listening") is True})
    subnets = []
    for item in (value.get("subnets") or [])[:MAX_SUBNETS]:
        if not isinstance(item, str) or len(item) > 64:
            raise ValueError("Invalid remote subnet.")
        network = ipaddress.ip_network(item, strict=False)
        if not network.is_private or network.prefixlen < (16 if network.version == 4 else 48):
            raise ValueError("Remote subnets must be private networks of at most 65,536 addresses.")
        subnets.append(str(network))
    subnet_ports = sorted({_port(item) for item in (value.get("subnet_ports") or [])[:32]})
    return {"enabled": enabled_flag, "shell": enabled_flag and value.get("shell") is True,
            "services": services if enabled_flag else [], "subnets": subnets if enabled_flag else [],
            "subnet_ports": subnet_ports if enabled_flag else [],
            "platform": str(value.get("platform") or "")[:40]}


def _number(value, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 <= value <= maximum:
        return None
    return value


def parse_system(value) -> dict | None:
    """Validate the ``system`` object of a heartbeat: memory, disk and GPU figures (bytes)."""
    if not isinstance(value, dict):
        return None
    limit = 2 ** 53
    result = {key: _number(value.get(key), limit) for key in
              ("ram_total", "ram_free", "vram_total", "vram_free", "disk_total", "disk_free")}
    result["cpu_count"] = _number(value.get("cpu_count"), 4096)
    features = value.get("features") if isinstance(value.get("features"), list) else []
    result["features"] = [item for item in features[:16] if isinstance(item, str) and re.fullmatch(r"[a-z_]{1,32}", item)]
    version = value.get("version")
    result["version"] = version[:20] if isinstance(version, str) else None
    return result


def remote_info(worker) -> dict:
    raw = worker["remote_info"] if worker is not None and "remote_info" in worker.keys() else None
    try:
        info = json.loads(raw) if raw else {}
    except ValueError:
        info = {}
    return info if isinstance(info, dict) else {}


def system_info(worker) -> dict:
    raw = worker["system_info"] if worker is not None and "system_info" in worker.keys() else None
    try:
        info = json.loads(raw) if raw else {}
    except ValueError:
        info = {}
    return info if isinstance(info, dict) else {}


def record(worker_id: str, remote, system) -> None:
    """Store what a heartbeat said (absent fields keep their previous values)."""
    store.set_remote_info(worker_id, json.dumps(remote) if remote is not None else None,
                          json.dumps(system) if system is not None else None)


def heartbeat_reply(worker_id: str, config=None) -> dict | None:
    """The ``remote`` part of a heartbeat reply: whether the worker should listen for sessions now."""
    if not enabled(config):
        return None
    row = store.remote_row(worker_id)
    if row is None or not remote_info(row).get("enabled"):
        return None
    now = time.time()
    armed = float(row["remote_armed_until"] or 0) > now or bool(store.live_tunnels(worker_id))
    return {"armed": armed, "poll_seconds": LONG_POLL_SECONDS}


def arm(worker_id: str | None = None) -> None:
    store.arm(worker_id, time.time() + ARM_SECONDS)


# ----- targets ---------------------------------------------------------------------------------------------------

def kind_for_port(port: int) -> str:
    if port == 22:
        return "ssh"
    if port == 3389:
        return "rdp"
    if 5900 <= port <= 5999:
        return "vnc"
    return "tcp"


def check_target(info: dict, kind: str, host: str, port: int) -> str | None:
    """Why the worker's owner does not allow this target (None: allowed). The worker checks it again."""
    if not info.get("enabled"):
        return "The worker's owner has not allowed remote access (BC_WORKER_REMOTE_ACCESS on the worker)."
    if kind == "shell":
        return None if info.get("shell") else ("The worker's owner has not allowed the built-in terminal "
                                               "(BC_WORKER_REMOTE_SHELL on the worker).")
    if host in LOCAL_HOSTS:
        if any(service["port"] == port for service in info.get("services", [])):
            return None
        return f"The worker does not offer port {port}. It allows: " + (", ".join(
            f"{service['kind']} {service['port']}" for service in info.get("services", [])) or "nothing") + "."
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "Use an IP address of the worker's network."
    if not any(address in ipaddress.ip_network(network) for network in info.get("subnets", [])):
        return "That address is not in a network the worker's owner shared."
    if port not in info.get("subnet_ports", []):
        return f"The worker's owner did not allow port {port} on that network."
    return None


# ----- virtual addresses ----------------------------------------------------------------------------------------

_address_lock = threading.Lock()


def virtual_ips(config=None) -> dict[str, str]:
    """``{worker id: address}``, assigning free addresses of ``BC_WORKER_SUBNET`` to workers without one."""
    network = ipaddress.IPv4Network(_config(config).worker_subnet)
    rows = store.remote_rows()
    if all(row["virtual_ip"] and _inside(row["virtual_ip"], network) for row in rows):
        return {row["id"]: row["virtual_ip"] for row in rows}  # nothing to assign: no write lock
    with _address_lock, db.transaction():
        rows = store.remote_rows()
        valid = {row["virtual_ip"] for row in rows if row["virtual_ip"] and _inside(row["virtual_ip"], network)}
        if any(row["virtual_ip"] and row["virtual_ip"] not in valid for row in rows):
            store.clear_virtual_ips_outside(valid)
            rows = store.remote_rows()
        used = store.used_virtual_ips()
        candidates = (str(address) for address in network.hosts() if int(address) - int(network.network_address) >= 2)
        result = {}
        for row in rows:
            address = row["virtual_ip"]
            if not address:
                address = next((candidate for candidate in candidates if candidate not in used), None)
                if address is None:
                    continue  # the network is full; such a worker is reached by name
                store.set_virtual_ip(row["id"], address)
                used.add(address)
            result[row["id"]] = address
    return result


def _inside(value: str, network) -> bool:
    try:
        return ipaddress.ip_address(value) in network
    except ValueError:
        return False


def slug(name: str) -> str:
    return _NAME_RE.sub("-", (name or "").lower()).strip("-")[:40] or "worker"


# ----- sessions ---------------------------------------------------------------------------------------------------

def _online(worker, now: float) -> bool:
    from bananachat.services.remote import HEARTBEAT_FRESH_SECONDS

    return worker["status"] != "disabled" and worker["last_heartbeat"] is not None \
        and float(worker["last_heartbeat"]) >= now - HEARTBEAT_FRESH_SECONDS


def create_session(worker_id: str, kind: str, host: str, port: int, *, created_by: str | None = None,
                   access_key_id: str | None = None, client_ip: str | None = None, config=None) -> dict:
    """Open a session to a worker's service; returns ``{"id", "worker", "kind", "host", "port"}``."""
    if not enabled(config):
        raise RemoteError("Remote access to workers is off on this server (BC_WORKER_REMOTE_ACCESS).")
    if kind not in KINDS:
        raise RemoteError("Unknown kind of session.")
    host = (host or "").strip().lower()
    if kind == "shell":
        host, port = "shell", 0
    else:
        host = "127.0.0.1" if host in ("", "localhost") else host
        try:
            port = _port(port)
        except ValueError:
            raise RemoteError("Choose a port from 1 to 65535.") from None
    now = time.time()
    tunnel_id = uuid.uuid4().hex
    with db.transaction():
        worker = store.remote_row(worker_id)
        if worker is None:
            raise RemoteError("No such worker.")
        if worker["status"] == "disabled":
            raise RemoteError("This worker is disabled.")
        if not _online(worker, now):
            raise RemoteError("The worker is offline. It must be running to be reached.")
        problem = check_target(remote_info(worker), kind, host, port)
        if problem:
            raise RemoteError(problem)
        if len(store.live_tunnels(worker_id)) >= 16:
            raise RemoteError("This worker already has 16 open sessions.")
        store.insert_tunnel(tunnel_id, worker_id, kind, host, port, now, created_by=created_by,
                            access_key_id=access_key_id, client_ip=client_ip)
        store.arm(worker_id, now + ARM_SECONDS)
    _announce()
    return {"id": tunnel_id, "worker": worker["name"], "worker_id": worker_id, "kind": kind, "host": host,
            "port": port}


_new_session = threading.Condition()


def _announce() -> None:
    with _new_session:
        _new_session.notify_all()


def _claim(worker_id: str) -> list[dict]:
    if not store.pending_tunnels(worker_id):
        return []  # the usual case while long-polling: no write lock
    now = time.time()
    claimed = []
    with db.transaction():
        for row in store.pending_tunnels(worker_id):
            if store.claim_tunnel(row["id"], worker_id, now):
                claimed.append({"id": row["id"], "kind": row["kind"], "host": row["target_host"],
                                "port": row["target_port"]})
    return claimed


_polls = {"active": 0}
_poll_lock = threading.Lock()


def poll(worker, *, wait: float = LONG_POLL_SECONDS, config=None) -> list[dict]:
    """Sessions waiting for this worker (claimed for it); long-polls up to *wait* seconds."""
    if not enabled(config):
        return []
    sessions = _claim(worker["id"])
    if sessions or wait <= 0:
        return sessions
    with _poll_lock:
        if _polls["active"] >= MAX_REMOTE_POLLS:
            return []
        _polls["active"] += 1
    try:
        deadline = time.monotonic() + min(wait, LONG_POLL_SECONDS)
        while not background.stopping():
            left = deadline - time.monotonic()
            if left <= 0:
                return []
            with _new_session:
                _new_session.wait(min(0.5, left))
            db.release_thread_connection()
            sessions = _claim(worker["id"])
            if sessions:
                return sessions
        return []
    finally:
        with _poll_lock:
            _polls["active"] -= 1


def session_for_worker(worker, tunnel_id: str):
    row = store.tunnel(tunnel_id)
    if row is None or row["worker_id"] != worker["id"] or row["status"] not in ("pending", "connecting"):
        return None
    return row


def session_for_client(tunnel_id: str, *, user_id: str | None = None, access_key_id: str | None = None):
    row = store.tunnel(tunnel_id)
    if row is None or row["status"] not in ("pending", "connecting"):
        return None
    if time.time() - float(row["created_at"]) > CLAIM_SECONDS + CONNECT_SECONDS:
        return None
    if access_key_id is not None:
        return row if row["access_key_id"] == access_key_id else None
    return row if user_id is not None and row["created_by"] == user_id and row["access_key_id"] is None else None


def _key_valid(row, now: float) -> bool:
    from bananachat.db import users

    return row is not None and row["revoked_at"] is None and float(row["expires_at"]) > now \
        and row["owner_role"] == "admin" and not users.is_suspended({"suspended": row["owner_suspended"],
                                                                     "suspended_until": row["owner_suspended_until"]})


def still_allowed(row, *, auth_session: str | None = None) -> bool:
    """Whether whoever opened a session may still use it: its access key is still valid, or the administrator who
    opened it in the browser still is one (not demoted, suspended or deleted) and is still signed in with
    *auth_session*, the login session (hash) whose WebSocket carries it: signing out, "sign out everywhere", a
    revoked session or a password change end the session too."""
    from bananachat.db import users

    if row["access_key_id"]:
        return _key_valid(store.key_by_id(row["access_key_id"]), time.time())
    user = users.get(row["created_by"]) if row["created_by"] else None
    return user is not None and user["role"] == "admin" and not users.is_suspended(user) \
        and auth_session is not None and store.login_session_live(auth_session, user["id"])


def opened(tunnel_id: str) -> None:
    store.open_tunnel(tunnel_id, time.time())


def ended(tunnel_id: str, *, ok: bool, error: str | None = None, up: int = 0, down: int = 0) -> None:
    try:
        store.end_tunnel(tunnel_id, "closed" if ok else "failed", time.time(), error=(error or None) and error[:300],
                         bytes_up=up, bytes_down=down)
    except Exception:  # noqa: BLE001 - the background job closes it later
        log.warning("Could not record the end of tunnel %s", tunnel_id[:8], exc_info=True)


def describe_tunnel(row) -> dict:
    return {key: row[key] for key in ("id", "worker_id", "kind", "target_host", "target_port", "status", "error",
                                      "created_at", "opened_at", "closed_at", "bytes_up", "bytes_down")}


# ----- access keys for the connect helper -------------------------------------------------------------------------

def create_key(user, label: str, hours: int) -> tuple[str, str]:
    label = " ".join(str(label or "").split())[:80]
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= KEY_MAX_HOURS:
        raise RemoteError(f"Choose a lifetime from 1 to {KEY_MAX_HOURS} hours.")
    key_id = uuid.uuid4().hex
    token = KEY_PREFIX + secrets.token_urlsafe(32)
    now = time.time()
    store.insert_key(key_id, worker_store.hash_token(token), label, user["id"], now, now + hours * 3600)
    return key_id, token


def authenticate_key(header: str | None):
    """The access key of an ``Authorization: Bearer bca_...`` header, while it and its administrator are valid."""

    scheme, _, token = (header or "").strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token.startswith(KEY_PREFIX) or len(token) > 200:
        return None
    row = store.key_by_hash(worker_store.hash_token(token))
    now = time.time()
    if not _key_valid(row, now):
        return None
    store.touch_key(row["id"], now)
    return row


def revoke_key(key_id: str) -> bool:
    now = time.time()
    with db.transaction():
        changed = store.revoke_key(key_id, now)
        store.end_key_tunnels(key_id, now)
    return bool(changed)


# ----- what the pages and the helper see --------------------------------------------------------------------------

def overview(config=None) -> list[dict]:
    """Every worker's remote access: allowed services, address and open sessions."""
    addresses = virtual_ips(config) if enabled(config) else {}
    now = time.time()
    live: dict[str, list] = {}
    for row in store.live_tunnels():
        live.setdefault(row["worker_id"], []).append(describe_tunnel(row))
    result = []
    for row in store.remote_rows():
        info = remote_info(row)
        result.append({"id": row["id"], "name": row["name"], "slug": slug(row["name"]),
                       "address": addresses.get(row["id"]), "online": _online(row, now),
                       "remote": info, "system": system_info(row),
                       "armed": float(row["remote_armed_until"] or 0) > now, "sessions": live.get(row["id"], [])})
    return result


def resolve(host: str, port: int, config=None):
    """The worker and target for an address the connect helper was asked to reach.

    *host* is a worker's virtual address, ``<name>.bc`` (or its name or id), or an address in a network exactly
    one worker shares. Returns ``(worker id, kind, host, port)``; raises :class:`RemoteError`.
    """
    host = (host or "").strip().lower().rstrip(".")
    workers = overview(config)
    by_address = {item["address"]: item for item in workers if item["address"]}
    target = by_address.get(host)
    if target is None:
        name = host[:-3] if host.endswith(".bc") else host
        target = next((item for item in workers if name in (item["slug"], item["id"], item["name"].lower())), None)
    if target is not None:
        return target["id"], kind_for_port(port), "127.0.0.1", port
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise RemoteError(f"No worker is called {host!r}.") from None
    # Each worker's owner chooses the networks it shares, so one worker may claim another's (a covering or a
    # narrower network, or simply the same 192.168.1.0/24): an address that more than one worker shares, online
    # or not, is never given to either of them, or a worker could receive sessions meant for another's network.
    matches = [item for item in workers
               if any(address in ipaddress.ip_network(network) for network in item["remote"].get("subnets", []))]
    if len(matches) > 1:
        names = ", ".join(sorted(item["name"] for item in matches))
        raise RemoteError(f"{host} is in networks shared by more than one worker ({names}). "
                          "Choose the worker: --worker NAME.")
    if matches:
        return matches[0]["id"], kind_for_port(port), str(address), port
    raise RemoteError(f"No worker shares a network containing {host}.")


# ----- housekeeping -------------------------------------------------------------------------------------------------

def maintain() -> dict:
    now = time.time()
    with db.transaction():
        expired = store.expire_tunnels(now - CLAIM_SECONDS, now - CONNECT_SECONDS, now)
        stale = store.close_stale_open(now - 13 * 3600, now)
        purged = store.delete_tunnels_before(now - KEEP_TUNNELS_SECONDS)
        keys = store.delete_old_keys(now - KEEP_TUNNELS_SECONDS)
    return {"expired": expired, "stale": stale, "purged": purged, "keys": keys}


@background.job("worker-tunnels", every=30, initial_delay=25)
def _maintain(app) -> None:
    result = maintain()
    if result["expired"] or result["stale"]:
        log.info("Worker tunnels: %(expired)d expired, %(stale)d ended by the time limit", result)
