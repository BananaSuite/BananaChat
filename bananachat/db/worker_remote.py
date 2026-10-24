"""Storage for maintenance access to worker PCs and for the model commands sent to them (SQL only).

Policy lives in ``services.worker_remote`` and ``services.worker_commands``.
Times are epoch seconds (REAL), like the rest of the worker tables.

* ``worker_tunnels``: remote sessions. ``pending`` (created, waiting for the
  worker), ``connecting`` (the worker took it), ``open`` (both sides joined),
  then ``closed`` or ``failed``.
* ``worker_access_keys``: keys of the connect helper (SHA-256 digests only).
* ``worker_commands``: ``queued`` -> ``running`` -> ``done`` | ``failed`` | ``cancelled``.
"""

from __future__ import annotations

from bananachat import db

TUNNEL_COLUMNS = ("id, worker_id, kind, target_host, target_port, status, created_by, access_key_id, client_ip, "
                  "created_at, claimed_at, opened_at, closed_at, error, bytes_up, bytes_down")
LIVE_TUNNELS = ("pending", "connecting", "open")
COMMAND_COLUMNS = ("id, worker_id, kind, model_name, status, progress_pct, detail, bytes_done, bytes_total, "
                   "requested_by, created_at, started_at, updated_at, finished_at, error")
LIVE_COMMANDS = ("queued", "running")


def _marks(values) -> str:
    return ",".join("?" for _ in values)


# ----- worker rows --------------------------------------------------------------------------------------------

def set_remote_info(worker_id: str, info_json: str | None, system_json: str | None) -> None:
    db.execute("UPDATE worker_nodes SET remote_info=COALESCE(?, remote_info), system_info=COALESCE(?, system_info) "
               "WHERE id=?", (info_json, system_json, worker_id))


def remote_row(worker_id: str):
    return db.one("SELECT id, name, status, last_heartbeat, platform, remote_info, remote_armed_until, virtual_ip, "
                  "system_info FROM worker_nodes WHERE id=?", (worker_id,))


def remote_rows():
    return db.query("SELECT id, name, status, last_heartbeat, platform, remote_info, remote_armed_until, virtual_ip, "
                    "system_info FROM worker_nodes ORDER BY registered_at, id")


def arm(worker_id: str | None, until: float) -> int:
    if worker_id is None:
        return db.execute("UPDATE worker_nodes SET remote_armed_until=MAX(COALESCE(remote_armed_until, 0), ?) "
                          "WHERE status<>'disabled'", (until,)).rowcount
    return db.execute("UPDATE worker_nodes SET remote_armed_until=MAX(COALESCE(remote_armed_until, 0), ?) "
                      "WHERE id=?", (until, worker_id)).rowcount


def used_virtual_ips() -> set[str]:
    return {row[0] for row in db.query("SELECT virtual_ip FROM worker_nodes WHERE virtual_ip IS NOT NULL")}


def set_virtual_ip(worker_id: str, address: str) -> int:
    return db.execute("UPDATE worker_nodes SET virtual_ip=? WHERE id=? AND virtual_ip IS NULL",
                      (address, worker_id)).rowcount


def clear_virtual_ips_outside(addresses) -> int:
    """Forget addresses that are not in the configured network any more (the next listing assigns new ones)."""
    addresses = list(addresses)
    if not addresses:
        return db.execute("UPDATE worker_nodes SET virtual_ip=NULL WHERE virtual_ip IS NOT NULL").rowcount
    return db.execute(f"UPDATE worker_nodes SET virtual_ip=NULL WHERE virtual_ip NOT IN ({_marks(addresses)})",
                      addresses).rowcount


# ----- tunnels ------------------------------------------------------------------------------------------------

def insert_tunnel(tunnel_id: str, worker_id: str, kind: str, host: str, port: int, now: float, *,
                  created_by: str | None, access_key_id: str | None, client_ip: str | None) -> None:
    db.execute("INSERT INTO worker_tunnels (id, worker_id, kind, target_host, target_port, status, created_by, "
               "access_key_id, client_ip, created_at) VALUES (?,?,?,?,?,'pending',?,?,?,?)",
               (tunnel_id, worker_id, kind, host, int(port), created_by, access_key_id, client_ip, now))


def tunnel(tunnel_id: str):
    return db.one(f"SELECT {TUNNEL_COLUMNS} FROM worker_tunnels WHERE id=?", (tunnel_id,))


def pending_tunnels(worker_id: str):
    return db.query(f"SELECT {TUNNEL_COLUMNS} FROM worker_tunnels WHERE worker_id=? AND status='pending' "
                    "ORDER BY created_at", (worker_id,))


def claim_tunnel(tunnel_id: str, worker_id: str, now: float) -> int:
    return db.execute("UPDATE worker_tunnels SET status='connecting', claimed_at=? WHERE id=? AND worker_id=? "
                      "AND status='pending'", (now, tunnel_id, worker_id)).rowcount


def open_tunnel(tunnel_id: str, now: float) -> int:
    return db.execute("UPDATE worker_tunnels SET status='open', opened_at=? WHERE id=? "
                      "AND status IN ('pending', 'connecting')", (now, tunnel_id)).rowcount


def end_tunnel(tunnel_id: str, status: str, now: float, *, error: str | None = None, bytes_up: int = 0,
               bytes_down: int = 0) -> int:
    return db.execute(f"UPDATE worker_tunnels SET status=?, closed_at=?, error=COALESCE(?, error), "
                      f"bytes_up=bytes_up+?, bytes_down=bytes_down+? WHERE id=? AND status IN ({_marks(LIVE_TUNNELS)})",
                      (status, now, error, int(bytes_up), int(bytes_down), tunnel_id, *LIVE_TUNNELS)).rowcount


def add_bytes(tunnel_id: str, bytes_up: int, bytes_down: int) -> None:
    db.execute("UPDATE worker_tunnels SET bytes_up=bytes_up+?, bytes_down=bytes_down+? WHERE id=?",
               (int(bytes_up), int(bytes_down), tunnel_id))


def live_tunnels(worker_id: str | None = None):
    sql = f"SELECT {TUNNEL_COLUMNS} FROM worker_tunnels WHERE status IN ({_marks(LIVE_TUNNELS)})"
    params: list = list(LIVE_TUNNELS)
    if worker_id is not None:
        sql += " AND worker_id=?"
        params.append(worker_id)
    return db.query(sql + " ORDER BY created_at", params)


def recent_tunnels(limit: int = 50):
    return db.query("SELECT t.*, w.name AS worker_name, u.username AS created_by_name FROM worker_tunnels t "
                    "LEFT JOIN worker_nodes w ON w.id=t.worker_id LEFT JOIN users u ON u.id=t.created_by "
                    "ORDER BY t.created_at DESC LIMIT ?", (limit,))


def expire_tunnels(unclaimed_before: float, unopened_before: float, now: float) -> int:
    count = db.execute("UPDATE worker_tunnels SET status='failed', closed_at=?, error=? WHERE status='pending' "
                       "AND created_at<?", (now, "The worker did not pick the session up in time.",
                                            unclaimed_before)).rowcount
    count += db.execute("UPDATE worker_tunnels SET status='failed', closed_at=?, error=? WHERE status='connecting' "
                        "AND COALESCE(claimed_at, created_at)<?",
                        (now, "The worker did not connect in time.", unopened_before)).rowcount
    return count


def close_stale_open(before: float, now: float) -> int:
    """Open sessions older than the longest allowed (their relay ended without saying so)."""
    return db.execute("UPDATE worker_tunnels SET status='closed', closed_at=?, error=COALESCE(error, ?) "
                      "WHERE status='open' AND opened_at<?", (now, "Ended by the time limit.", before)).rowcount


def end_worker_tunnels(worker_id: str, error: str, now: float) -> int:
    return db.execute(f"UPDATE worker_tunnels SET status='failed', closed_at=?, error=? WHERE worker_id=? "
                      f"AND status IN ({_marks(LIVE_TUNNELS)})", (now, error, worker_id, *LIVE_TUNNELS)).rowcount


def delete_tunnels_before(before: float) -> int:
    return db.execute(f"DELETE FROM worker_tunnels WHERE status NOT IN ({_marks(LIVE_TUNNELS)}) "
                      "AND COALESCE(closed_at, created_at)<?", (*LIVE_TUNNELS, before)).rowcount


# ----- access keys --------------------------------------------------------------------------------------------

def insert_key(key_id: str, token_hash: str, label: str, created_by: str, now: float, expires_at: float) -> None:
    db.execute("INSERT INTO worker_access_keys (id, token_hash, label, created_by, created_at, expires_at) "
               "VALUES (?,?,?,?,?,?)", (key_id, token_hash, label, created_by, now, expires_at))


def key_by_hash(token_hash: str):
    return db.one("SELECT k.*, u.role AS owner_role, u.suspended AS owner_suspended, u.suspended_until AS "
                  "owner_suspended_until FROM worker_access_keys k "
                  "LEFT JOIN users u ON u.id=k.created_by WHERE k.token_hash=?", (token_hash,))


def key_by_id(key_id: str):
    return db.one("SELECT k.*, u.role AS owner_role, u.suspended AS owner_suspended, u.suspended_until AS "
                  "owner_suspended_until FROM worker_access_keys k "
                  "LEFT JOIN users u ON u.id=k.created_by WHERE k.id=?", (key_id,))


def keys():
    return db.query("SELECT k.*, u.username AS created_by_name FROM worker_access_keys k "
                    "LEFT JOIN users u ON u.id=k.created_by ORDER BY k.created_at DESC")


def touch_key(key_id: str, now: float) -> None:
    db.execute("UPDATE worker_access_keys SET last_used_at=? WHERE id=? AND (last_used_at IS NULL OR last_used_at<?)",
               (now, key_id, now - 60))


def revoke_key(key_id: str, now: float) -> int:
    return db.execute("UPDATE worker_access_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                      (now, key_id)).rowcount


def end_key_tunnels(key_id: str, now: float) -> int:
    return db.execute(f"UPDATE worker_tunnels SET status='failed', closed_at=?, error=? WHERE access_key_id=? "
                      f"AND status IN ({_marks(LIVE_TUNNELS)})",
                      (now, "The access key was revoked.", key_id, *LIVE_TUNNELS)).rowcount


def delete_old_keys(before: float) -> int:
    return db.execute("DELETE FROM worker_access_keys WHERE COALESCE(revoked_at, expires_at)<?", (before,)).rowcount


# ----- model commands -----------------------------------------------------------------------------------------

def insert_command(command_id: str, worker_id: str, kind: str, model: str, requested_by: str | None,
                   now: float) -> None:
    db.execute("INSERT INTO worker_commands (id, worker_id, kind, model_name, status, requested_by, created_at, "
               "updated_at, detail) VALUES (?,?,?,?,'queued',?,?,?,?)",
               (command_id, worker_id, kind, model, requested_by, now, now, "Waiting for the worker."))


def command(command_id: str):
    return db.one(f"SELECT {COMMAND_COLUMNS} FROM worker_commands WHERE id=?", (command_id,))


def live_command_for(worker_id: str, model: str):
    return db.one(f"SELECT {COMMAND_COLUMNS} FROM worker_commands WHERE worker_id=? AND model_name=? "
                  f"AND status IN ({_marks(LIVE_COMMANDS)})", (worker_id, model, *LIVE_COMMANDS))


def queued_commands(worker_id: str, limit: int = 4):
    return db.query(f"SELECT {COMMAND_COLUMNS} FROM worker_commands WHERE worker_id=? AND status='queued' "
                    "ORDER BY created_at LIMIT ?", (worker_id, limit))


def running_commands(worker_id: str):
    return db.query(f"SELECT {COMMAND_COLUMNS} FROM worker_commands WHERE worker_id=? AND status='running'",
                    (worker_id,))


def start_command(command_id: str, worker_id: str, now: float) -> int:
    return db.execute("UPDATE worker_commands SET status='running', started_at=?, updated_at=?, detail=? "
                      "WHERE id=? AND worker_id=? AND status='queued'",
                      (now, now, "Starting…", command_id, worker_id)).rowcount


def update_command(command_id: str, worker_id: str, now: float, *, percent: int, detail: str,
                   bytes_done: int | None, bytes_total: int | None) -> int:
    return db.execute("UPDATE worker_commands SET progress_pct=?, detail=?, bytes_done=COALESCE(?, bytes_done), "
                      "bytes_total=COALESCE(?, bytes_total), updated_at=? WHERE id=? AND worker_id=? "
                      "AND status='running'", (max(0, min(100, int(percent))), detail[:500], bytes_done, bytes_total,
                                               now, command_id, worker_id)).rowcount


def finish_command(command_id: str, status: str, now: float, *, error: str | None = None,
                   detail: str | None = None, only_from=LIVE_COMMANDS) -> int:
    progress = ", progress_pct=100" if status == "done" else ""
    return db.execute(f"UPDATE worker_commands SET status=?, finished_at=?, updated_at=?, error=?, "
                      f"detail=COALESCE(?, detail){progress} WHERE id=? AND status IN ({_marks(only_from)})",
                      (status, now, now, error, detail, command_id, *only_from)).rowcount


def commands(worker_id: str | None = None, limit: int = 100):
    sql = f"SELECT c.{COMMAND_COLUMNS.replace(', ', ', c.')}, w.name AS worker_name FROM worker_commands c " \
          "LEFT JOIN worker_nodes w ON w.id=c.worker_id"
    params: list = []
    if worker_id is not None:
        sql += " WHERE c.worker_id=?"
        params.append(worker_id)
    return db.query(sql + f" ORDER BY c.status IN ({_marks(LIVE_COMMANDS)}) DESC, c.created_at DESC LIMIT ?",
                    [*params, *LIVE_COMMANDS, limit])


def live_commands():
    return db.query(f"SELECT {COMMAND_COLUMNS} FROM worker_commands WHERE status IN ({_marks(LIVE_COMMANDS)})",
                    LIVE_COMMANDS)


def delete_finished_commands(before: float) -> int:
    return db.execute(f"DELETE FROM worker_commands WHERE status NOT IN ({_marks(LIVE_COMMANDS)}) "
                      "AND COALESCE(finished_at, created_at)<?", (*LIVE_COMMANDS, before)).rowcount
