-- Pre-versioned schema baseline; imported atomically by legacy.py.
-- =====================================================================
--  Users & Auth
-- =====================================================================
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password        TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'user'
                        CHECK(role IN ('user', 'admin')),
    suspended       INTEGER NOT NULL DEFAULT 0,
    suspended_until TEXT,
    invite_code     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at   TEXT,
    accessibility   TEXT
);

CREATE TABLE IF NOT EXISTS invite_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT    NOT NULL UNIQUE,
    created_by      TEXT    REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT,
    max_uses        INTEGER NOT NULL DEFAULT 1,
    use_count       INTEGER NOT NULL DEFAULT 0,
    assigned_role   TEXT,
    deleted         INTEGER NOT NULL DEFAULT 0,
    deleted_at      TEXT
);

CREATE TABLE IF NOT EXISTS invite_code_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invite_code_id  INTEGER NOT NULL REFERENCES invite_codes(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    used_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address  TEXT NOT NULL,
    attempted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rate_limit_hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,
    hit_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- =====================================================================
--  Site Settings (singleton row id=1)
-- =====================================================================
CREATE TABLE IF NOT EXISTS site_settings (
    id                  INTEGER PRIMARY KEY DEFAULT 1,
    site_name           TEXT    NOT NULL DEFAULT 'BananaChat',
    signup_mode         TEXT    NOT NULL DEFAULT 'invite'
                            CHECK(signup_mode IN ('invite', 'open', 'disabled')),
    maintenance_mode    INTEGER NOT NULL DEFAULT 0,
    maintenance_message TEXT    NOT NULL DEFAULT '',
    setup_done          INTEGER NOT NULL DEFAULT 0,
    default_theme_mode  TEXT    NOT NULL DEFAULT 'dark',
    primary_color       TEXT    NOT NULL DEFAULT '#e6be32',
    secondary_color     TEXT    NOT NULL DEFAULT '#1d1d1d',
    accent_color        TEXT    NOT NULL DEFAULT '#cda624',
    text_color          TEXT    NOT NULL DEFAULT '#ededed',
    sidebar_color       TEXT    NOT NULL DEFAULT '#181818',
    bg_color            TEXT    NOT NULL DEFAULT '#141414',
    light_primary_color TEXT    NOT NULL DEFAULT '#8a6500',
    light_secondary_color TEXT  NOT NULL DEFAULT '#ffffff',
    light_accent_color  TEXT    NOT NULL DEFAULT '#6f5000',
    light_text_color    TEXT    NOT NULL DEFAULT '#202124',
    light_sidebar_color TEXT    NOT NULL DEFAULT '#f4f1e8',
    light_bg_color      TEXT    NOT NULL DEFAULT '#faf9f5'
);

-- =====================================================================
--  AI Model Catalog
-- =====================================================================
CREATE TABLE IF NOT EXISTS ai_models (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ollama_name     TEXT NOT NULL UNIQUE,
    backend         TEXT NOT NULL DEFAULT 'ollama'
                        CHECK(backend IN ('ollama', 'comfyui')),
    backend_model_name TEXT,
    backend_available INTEGER NOT NULL DEFAULT 1
                        CHECK(backend_available IN (0, 1)),
    backend_last_seen_at TEXT,
    display_name    TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    is_rolled_out   INTEGER NOT NULL DEFAULT 0,
    is_uncensored   INTEGER NOT NULL DEFAULT 0,
    is_image_generation INTEGER NOT NULL DEFAULT 0,
    supports_vision INTEGER NOT NULL DEFAULT 0,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS model_categories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    scope           TEXT NOT NULL DEFAULT 'both'
                        CHECK(scope IN ('chat', 'api', 'both')),
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS model_category_assignments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id        INTEGER NOT NULL REFERENCES ai_models(id) ON DELETE CASCADE,
    category_id     INTEGER NOT NULL REFERENCES model_categories(id) ON DELETE CASCADE,
    UNIQUE(model_id, category_id)
);

-- Generic model-access gates. Capability scopes use resource_id=0;
-- category and model resources are cleaned up by triggers below.
CREATE TABLE IF NOT EXISTS model_access_policies (
    scope           TEXT NOT NULL
                        CHECK(scope IN ('uncensored', 'image_generation', 'custom_personality', 'category', 'model')),
    resource_id     INTEGER NOT NULL DEFAULT 0,
    mode            TEXT NOT NULL DEFAULT 'allow_all'
                        CHECK(mode IN ('allow_all', 'deny_except_allowlist', 'allow_except_denylist')),
    requests_enabled INTEGER NOT NULL DEFAULT 0
                        CHECK(requests_enabled IN (0, 1)),
    updated_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(scope, resource_id),
    CHECK(
        (scope IN ('uncensored', 'image_generation', 'custom_personality') AND resource_id=0)
        OR (scope IN ('category', 'model') AND resource_id>0)
    )
);

CREATE TABLE IF NOT EXISTS model_access_memberships (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope           TEXT NOT NULL,
    resource_id     INTEGER NOT NULL,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    list_type       TEXT NOT NULL CHECK(list_type IN ('allowlist', 'denylist')),
    added_by        TEXT REFERENCES users(id) ON DELETE SET NULL,
    reason          TEXT NOT NULL DEFAULT '',
    expires_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(scope, resource_id, user_id, list_type),
    FOREIGN KEY(scope, resource_id)
        REFERENCES model_access_policies(scope, resource_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS model_access_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope           TEXT NOT NULL,
    resource_id     INTEGER NOT NULL,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    use_case        TEXT NOT NULL DEFAULT '',
    confirmed_safe  INTEGER NOT NULL CHECK(confirmed_safe IN (0, 1)),
    confirmed_logging INTEGER NOT NULL CHECK(confirmed_logging IN (0, 1)),
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'approved', 'denied')),
    admin_message   TEXT,
    resolved_by     TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT,
    FOREIGN KEY(scope, resource_id)
        REFERENCES model_access_policies(scope, resource_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_model_access_memberships_user
    ON model_access_memberships(user_id, scope, resource_id);
CREATE INDEX IF NOT EXISTS idx_model_access_requests_status
    ON model_access_requests(status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_access_requests_one_pending
    ON model_access_requests(scope, resource_id, user_id)
    WHERE status='pending';

-- =====================================================================
--  API Tokens
-- =====================================================================
CREATE TABLE IF NOT EXISTS api_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL DEFAULT '',
    token_hash      TEXT NOT NULL UNIQUE,
    token_prefix    TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at    TEXT,
    revoked         INTEGER NOT NULL DEFAULT 0
);

-- =====================================================================
--  Credits & Quotas
-- =====================================================================
CREATE TABLE IF NOT EXISTS user_quota (
    user_id             TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    daily_credits       INTEGER NOT NULL DEFAULT 30,
    daily_slow_credits  INTEGER NOT NULL DEFAULT 15,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by          TEXT REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_id        INTEGER REFERENCES api_tokens(id) ON DELETE SET NULL,
    model_id        INTEGER REFERENCES ai_models(id) ON DELETE SET NULL,
    credits_used    REAL NOT NULL DEFAULT 0,
    is_slow         INTEGER NOT NULL DEFAULT 0,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    request_type    TEXT NOT NULL DEFAULT 'api'
                        CHECK(request_type IN ('api', 'playground', 'chat', 'chat_incognito')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS image_credit_reservations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_id        INTEGER REFERENCES api_tokens(id) ON DELETE SET NULL,
    model_id        INTEGER REFERENCES ai_models(id) ON DELETE SET NULL,
    credits_reserved REAL NOT NULL,
    is_slow         INTEGER NOT NULL DEFAULT 0 CHECK(is_slow IN (0, 1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS quota_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    new_credits     INTEGER NOT NULL,
    new_slow_credits INTEGER NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    duration_type   TEXT NOT NULL DEFAULT 'permanent'
                        CHECK(duration_type IN ('one_day', 'permanent')),
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'approved', 'denied')),
    admin_message   TEXT,
    resolution_source TEXT NOT NULL DEFAULT 'manual'
                          CHECK(resolution_source IN ('manual', 'automatic')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT,
    resolved_by     TEXT REFERENCES users(id) ON DELETE SET NULL
);

-- =====================================================================
--  Custom Personalities
-- =====================================================================
CREATE TABLE IF NOT EXISTS personalities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    instructions    TEXT NOT NULL,
    is_enabled      INTEGER NOT NULL DEFAULT 1 CHECK(is_enabled IN (0, 1)),
    admin_disabled  INTEGER NOT NULL DEFAULT 0 CHECK(admin_disabled IN (0, 1)),
    disabled_until  TEXT,
    disabled_reason TEXT NOT NULL DEFAULT '',
    created_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    updated_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_personalities_user_name
    ON personalities(user_id, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_personalities_user
    ON personalities(user_id, updated_at);

-- =====================================================================
--  Chat
-- =====================================================================
CREATE TABLE IF NOT EXISTS chat_sessions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT NOT NULL DEFAULT 'New Chat',
    is_incognito    INTEGER NOT NULL DEFAULT 0,
    personality_id  INTEGER REFERENCES personalities(id) ON DELETE SET NULL,
    shared_token    TEXT UNIQUE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL DEFAULT '',
    model_id        INTEGER REFERENCES ai_models(id) ON DELETE SET NULL,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_attachments (
    id              TEXT PRIMARY KEY,
    message_id      INTEGER NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK(kind IN ('image', 'pdf', 'text', 'code')),
    filename        TEXT NOT NULL,
    media_type      TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    extracted_text  TEXT,
    image_data      BLOB,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(
        (kind='image' AND image_data IS NOT NULL AND extracted_text IS NULL)
        OR (kind!='image' AND image_data IS NULL AND extracted_text IS NOT NULL)
    )
);

-- =====================================================================
--  Incognito Audit (separate table for admin review)
-- =====================================================================
CREATE TABLE IF NOT EXISTS incognito_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    model_id        INTEGER REFERENCES ai_models(id) ON DELETE SET NULL,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- =====================================================================
--  Compute Metrics
-- =====================================================================
CREATE TABLE IF NOT EXISTS compute_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gpu_name        TEXT,
    gpu_memory_used_mb  REAL,
    gpu_memory_total_mb REAL,
    gpu_utilization_percent REAL,
    system_memory_used_mb  REAL,
    system_memory_total_mb REAL,
    metrics_source  TEXT,
    cpu_percent     REAL,
    active_models   TEXT,
    queue_depth     INTEGER NOT NULL DEFAULT 0,
    recorded_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS request_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_type    TEXT NOT NULL,
    model_id        INTEGER REFERENCES ai_models(id) ON DELETE SET NULL,
    user_id         TEXT REFERENCES users(id) ON DELETE SET NULL,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    queue_wait_ms   INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'ok',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cross-worker runtime coordination. These rows are leases, not
-- durable application data; stale owners are reclaimed by callers.
CREATE TABLE IF NOT EXISTS inference_queue (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    req_id          TEXT NOT NULL UNIQUE,
    priority        INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'waiting'
                        CHECK(status IN ('waiting', 'running')),
    owner_pid       INTEGER,
    enqueued_at     REAL NOT NULL,
    started_at      REAL,
    heartbeat_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS active_streams (
    session_id      TEXT PRIMARY KEY,
    owner_token     TEXT NOT NULL,
    stop_requested  INTEGER NOT NULL DEFAULT 0,
    heartbeat_at    REAL NOT NULL
);

-- =====================================================================
--  Model Pull Queue
-- =====================================================================
CREATE TABLE IF NOT EXISTS model_pull_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ollama_name     TEXT NOT NULL,
    backend         TEXT NOT NULL DEFAULT 'ollama'
                        CHECK(backend IN ('ollama', 'comfyui')),
    repo_id         TEXT,
    source_filename TEXT,
    revision        TEXT,
    target_name     TEXT,
    expected_sha256 TEXT,
    expected_size   INTEGER,
    remote_job_id   TEXT,
    idempotency_key TEXT UNIQUE,
    status          TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN ('queued', 'pulling', 'done', 'failed', 'cancelled')),
    progress_pct    INTEGER NOT NULL DEFAULT 0,
    progress_detail TEXT NOT NULL DEFAULT '',
    error_message   TEXT,
    requested_by    TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    started_at      TEXT,
    finished_at     TEXT
);

-- =====================================================================
--  Indexes
-- =====================================================================
CREATE INDEX IF NOT EXISTS idx_credit_ledger_user_date ON credit_ledger(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_token ON credit_ledger(token_id);
CREATE INDEX IF NOT EXISTS idx_image_credit_reservations_user
    ON image_credit_reservations(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_user ON chat_sessions(user_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_attachments_message ON chat_attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON api_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_request_metrics_date ON request_metrics(created_at);
CREATE INDEX IF NOT EXISTS idx_compute_snapshots_date ON compute_snapshots(recorded_at);
CREATE INDEX IF NOT EXISTS idx_incognito_audit_user ON incognito_audit(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address, attempted_at);
CREATE INDEX IF NOT EXISTS idx_rate_limit_key ON rate_limit_hits(key, hit_at);
CREATE INDEX IF NOT EXISTS idx_inference_queue_order
    ON inference_queue(status, priority, seq);
CREATE INDEX IF NOT EXISTS idx_inference_queue_heartbeat
    ON inference_queue(heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_active_streams_heartbeat
    ON active_streams(heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_pull_jobs_status
    ON model_pull_jobs(status, created_at);

-- =====================================================================
--  Worker Nodes (personal-PC / remote-GPU workers)
-- =====================================================================
CREATE TABLE IF NOT EXISTS worker_nodes (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    platform        TEXT,
    gpu_name        TEXT,
    gpu_util        REAL,
    ollama_version  TEXT,
    capabilities    TEXT,           -- JSON: {"models": [...]}
    activity_state  TEXT,           -- idle/light/active/gaming
    status          TEXT NOT NULL DEFAULT 'offline'
                        CHECK(status IN ('online', 'busy', 'offline', 'disabled')),
    last_heartbeat  REAL,
    registered_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Jobs dispatched to remote workers
CREATE TABLE IF NOT EXISTS worker_jobs (
    id              TEXT PRIMARY KEY,
    worker_id       TEXT REFERENCES worker_nodes(id) ON DELETE SET NULL,
    model_name      TEXT NOT NULL,
    messages        TEXT NOT NULL,  -- JSON
    options         TEXT,           -- JSON
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','claimed','streaming','done','failed','timeout')),
    priority        INTEGER NOT NULL DEFAULT 2,
    stop_requested  INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    claimed_at      REAL,
    done_at         REAL,
    heartbeat_at    REAL,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT
);

-- Streaming chunks relayed from workers (temporary ring buffer)
CREATE TABLE IF NOT EXISTS worker_job_chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES worker_jobs(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    done            INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    UNIQUE(job_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_worker_jobs_status
    ON worker_jobs(status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_worker_jobs_heartbeat
    ON worker_jobs(heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_worker_chunks_job
    ON worker_job_chunks(job_id, seq);
CREATE INDEX IF NOT EXISTS idx_worker_nodes_heartbeat
    ON worker_nodes(status, last_heartbeat);
