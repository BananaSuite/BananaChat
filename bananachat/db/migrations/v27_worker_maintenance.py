"""Version 27: maintenance access and model management for worker PCs, reasoning on workers, local API servers.

Additive only, and safe to run again.

* ``worker_nodes.remote_info``: what the worker's owner allows administrators to
  reach (JSON, from its heartbeat); ``remote_armed_until``: until when the
  worker listens for new remote sessions without waiting for a heartbeat;
  ``virtual_ip``: its address in ``BC_WORKER_SUBNET`` (unique when set);
  ``system_info``: memory, disk and GPU figures (JSON, from its heartbeat).
* ``worker_tunnels``: one row per remote session (a terminal, SSH, VNC or RDP
  connection relayed between an administrator and a worker).
* ``worker_access_keys``: keys of the connect helper administrators use for
  native SSH, VNC and RDP clients (only their SHA-256 digest is stored).
* ``worker_commands``: model downloads and removals an administrator asked a
  worker to run, with their progress.
* ``worker_jobs.think`` and ``worker_job_chunks.thinking``: reasoning requested
  from, and relayed by, workers that support it.
* ``external_providers.local_backend``: an OpenAI-compatible server on the
  operator's own hardware (LM Studio, llama.cpp, vLLM) counts as local.
* BM mode (the easy mode, a simpler interface): ``site_settings.bm_mode_global``
  (on for everyone; off by default) and ``bm_mode_self_service`` (people may
  switch it themselves; on by default), and per account ``users.bm_mode`` and
  ``users.bm_mode_control`` (``site``: the site's rule, ``self``: the person
  may switch it, ``admin``: only administrators).
* ``site_settings.easter_mascot_motion``, ``easter_mascot_shades`` and
  ``easter_konami``: the easter eggs the site offers (all on, as before).
* ``inference_queue.lane``: hosted models (external providers, Claude, Codex)
  wait and run in a lane of their own, so requests stuck on an unreachable
  model server or worker never hold them up.
"""

from sqlite_migrations import add_columns, execute_script


def upgrade(conn):
    add_columns(conn, "worker_nodes", {
        "remote_info": "TEXT",
        "remote_armed_until": "REAL",
        "virtual_ip": "TEXT",
        "system_info": "TEXT",
    })
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_worker_nodes_virtual_ip ON worker_nodes(virtual_ip) "
                 "WHERE virtual_ip IS NOT NULL")
    execute_script(conn, """
        CREATE TABLE IF NOT EXISTS worker_access_keys (
            id            TEXT PRIMARY KEY,
            token_hash    TEXT NOT NULL UNIQUE,
            label         TEXT NOT NULL DEFAULT '',
            created_by    TEXT REFERENCES users(id) ON DELETE CASCADE,
            created_at    REAL NOT NULL,
            expires_at    REAL NOT NULL,
            revoked_at    REAL,
            last_used_at  REAL
        );
        CREATE TABLE IF NOT EXISTS worker_tunnels (
            id            TEXT PRIMARY KEY,
            worker_id     TEXT NOT NULL REFERENCES worker_nodes(id) ON DELETE CASCADE,
            kind          TEXT NOT NULL CHECK(kind IN ('shell', 'ssh', 'vnc', 'rdp', 'tcp')),
            target_host   TEXT NOT NULL,
            target_port   INTEGER NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending'
                          CHECK(status IN ('pending', 'connecting', 'open', 'closed', 'failed')),
            created_by    TEXT REFERENCES users(id) ON DELETE SET NULL,
            access_key_id TEXT REFERENCES worker_access_keys(id) ON DELETE CASCADE,
            client_ip     TEXT,
            created_at    REAL NOT NULL,
            claimed_at    REAL,
            opened_at     REAL,
            closed_at     REAL,
            error         TEXT,
            bytes_up      INTEGER NOT NULL DEFAULT 0,
            bytes_down    INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_worker_tunnels_worker ON worker_tunnels(worker_id, status);
        CREATE INDEX IF NOT EXISTS idx_worker_tunnels_created ON worker_tunnels(created_at);
        CREATE TABLE IF NOT EXISTS worker_commands (
            id            TEXT PRIMARY KEY,
            worker_id     TEXT NOT NULL REFERENCES worker_nodes(id) ON DELETE CASCADE,
            kind          TEXT NOT NULL CHECK(kind IN ('pull', 'delete')),
            model_name    TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'queued'
                          CHECK(status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
            progress_pct  INTEGER NOT NULL DEFAULT 0,
            detail        TEXT NOT NULL DEFAULT '',
            bytes_done    INTEGER,
            bytes_total   INTEGER,
            requested_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
            created_at    REAL NOT NULL,
            started_at    REAL,
            updated_at    REAL NOT NULL,
            finished_at   REAL,
            error         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_worker_commands_worker ON worker_commands(worker_id, status);
    """)
    add_columns(conn, "inference_queue", {"lane": "TEXT NOT NULL DEFAULT 'local'"})
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inference_queue_lane ON inference_queue(lane, status)")
    add_columns(conn, "worker_jobs", {"think": "TEXT"})
    add_columns(conn, "worker_job_chunks", {"thinking": "TEXT NOT NULL DEFAULT ''"})
    add_columns(conn, "external_providers", {
        "local_backend": "INTEGER NOT NULL DEFAULT 0 CHECK(local_backend IN (0,1))",
    })
    add_columns(conn, "site_settings", {
        "bm_mode_global": "INTEGER NOT NULL DEFAULT 0 CHECK(bm_mode_global IN (0,1))",
        "bm_mode_self_service": "INTEGER NOT NULL DEFAULT 1 CHECK(bm_mode_self_service IN (0,1))",
        "easter_mascot_motion": "INTEGER NOT NULL DEFAULT 1 CHECK(easter_mascot_motion IN (0,1))",
        "easter_mascot_shades": "INTEGER NOT NULL DEFAULT 1 CHECK(easter_mascot_shades IN (0,1))",
        "easter_konami": "INTEGER NOT NULL DEFAULT 1 CHECK(easter_konami IN (0,1))",
    })
    add_columns(conn, "users", {
        "bm_mode": "INTEGER NOT NULL DEFAULT 0 CHECK(bm_mode IN (0,1))",
        "bm_mode_control": "TEXT NOT NULL DEFAULT 'site' CHECK(bm_mode_control IN ('site', 'self', 'admin'))",
    })
    # Providers made from the local presets before this version run on the operator's own hardware.
    conn.execute("UPDATE external_providers SET local_backend=1 WHERE preset IN ('lmstudio', 'vllm', 'llamacpp')")
