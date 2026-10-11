"""Removing the Claude subscription connector upgrades existing databases in place.

The connector's account tables and runtime state are dropped. Catalog models
it served are withdrawn (hidden from pickers and the API) but kept, so chats
that used them still show their history and model name.
"""

import json
import sqlite3
from contextlib import closing

from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION
from bananachat.db.migrations.v21_remove_claude_connector import upgrade
from sqlite_migrations import apply_migrations

CONNECTOR_KEYS = ("claude_pool_quota", "claude.account_reports", "claude.selected_models", "claude.auto_enroll",
                  "claude.excluded_models", "claude_code_profiles")


def _v20_database(path):
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:20])
    conn.execute("INSERT INTO users(id,username,password) VALUES ('u1','existing','fixture')")
    conn.execute("INSERT INTO ai_models(id,ollama_name,backend,backend_model_name,display_name,is_rolled_out,"
                 "provider,family) VALUES (1,'claude-sonnet','claude','claude-sonnet','Claude Sonnet',1,'claude',"
                 "'sonnet')")
    conn.execute("INSERT INTO ai_models(id,ollama_name,backend,display_name,is_rolled_out,retired_at,state_reason) "
                 "VALUES (2,'claude-opus','claude','Claude Opus',0,'2020-01-01 00:00:00','Retired earlier.')")
    conn.execute("INSERT INTO ai_models(id,ollama_name,backend,display_name,is_rolled_out) "
                 "VALUES (3,'llama3.2:3b','ollama','Llama',1)")
    conn.execute("INSERT INTO personalities(id,user_id,name,instructions,preferred_model) VALUES "
                 "(1,'u1','Writer','Write.','claude-sonnet'), (2,'u1','Local','Talk.','llama3.2:3b')")
    conn.execute("INSERT INTO chat_sessions(id,user_id,title) VALUES ('c1','u1','Earlier chat')")
    conn.execute("INSERT INTO chat_messages(session_id,role,content,model_id) VALUES "
                 "('c1','user','Hello',NULL), ('c1','assistant','Hi from Claude',1)")
    conn.execute("INSERT INTO claude_accounts(id,label) VALUES (1,'claude-1')")
    conn.execute("INSERT INTO claude_account_leases(account_id,lease_id,heartbeat_at,expires_at) "
                 "VALUES (1,'lease',0,0)")
    conn.execute("INSERT INTO claude_account_usage(lease_id,account_id,tokens_in,tokens_out) VALUES ('lease',1,1,2)")
    for key in (*CONNECTOR_KEYS, "unrelated.state"):
        conn.execute("INSERT INTO runtime_state(key,value,updated_at) VALUES (?,?,0)", (key, json.dumps({"x": 1})))
    return conn


def test_upgrade_drops_connector_storage_and_withdraws_its_models(tmp_path):
    with closing(_v20_database(tmp_path / "v20.db")) as conn:
        messages = conn.execute("SELECT id,role,content,model_id FROM chat_messages ORDER BY id").fetchall()
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%claude%'").fetchall() == []
        assert conn.execute("SELECT key FROM runtime_state").fetchall() == [("unrelated.state",)]

        withdrawn = conn.execute("SELECT is_rolled_out,backend_available,retired_at IS NOT NULL,"
                                 "deprecated_at IS NOT NULL,state_reason FROM ai_models WHERE id=1").fetchone()
        assert withdrawn[:4] == (0, 0, 1, 1)
        assert "removed" in withdrawn[4]
        # An already retired model keeps its original retirement date.
        assert conn.execute("SELECT retired_at,backend_available FROM ai_models WHERE id=2").fetchone() == \
            ("2020-01-01 00:00:00", 0)
        assert conn.execute("SELECT is_rolled_out,backend_available,retired_at FROM ai_models WHERE id=3"
                            ).fetchone() == (1, 1, None)
        events = conn.execute("SELECT model_id,model_name,event FROM model_lifecycle_events ORDER BY model_id"
                              ).fetchall()
        assert events == [(1, "claude-sonnet", "retired"), (2, "claude-opus", "retired")]

        # Chats keep their history and the model it came from.
        assert conn.execute("SELECT id,role,content,model_id FROM chat_messages ORDER BY id").fetchall() == messages
        assert conn.execute("SELECT preferred_model FROM personalities ORDER BY id").fetchall() == \
            [("",), ("llama3.2:3b",)]

        # Safe to run again.
        upgrade(conn)
        assert conn.execute("SELECT COUNT(*) FROM model_lifecycle_events").fetchone()[0] == 2
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_new_databases_have_no_connector_tables(tmp_path):
    with closing(sqlite3.connect(tmp_path / "new.db")) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%claude%'").fetchall() == []
