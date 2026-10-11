"""Version 25 upgrades in place: running answers keep their account, workers and jobs start with a clean record,
attachments are measured by what they store."""

import sqlite3
from contextlib import closing

from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION
from bananachat.db.migrations.v25_run_owner_worker_backoff import upgrade
from sqlite_migrations import apply_migrations


def test_v24_upgrade_names_the_account_of_running_answers(tmp_path):
    with closing(sqlite3.connect(tmp_path / "v24.db")) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:24])
        conn.execute("INSERT INTO users(id,username,password) VALUES ('alice','alice','fixture')")
        for session_id, incognito in (("normal", 0), ("private", 1)):
            conn.execute("INSERT INTO chat_sessions(id,user_id,title,is_incognito) VALUES (?,?,?,?)",
                         (session_id, "alice", "Chat", incognito))
            conn.execute("INSERT INTO active_streams(session_id,owner_token,heartbeat_at) VALUES (?,?,1)",
                         (session_id, "token-" + session_id))
        conn.execute("INSERT INTO active_streams(session_id,owner_token,heartbeat_at) VALUES ('erased','t',1)")
        conn.execute("INSERT INTO worker_nodes(id,name,token_hash) VALUES ('w','PC','hash')")
        conn.execute("INSERT INTO worker_jobs(id,worker_id,model_name,messages,created_at) VALUES ('j','w','m','[]',1)")
        message = conn.execute("INSERT INTO chat_messages(session_id,role,content) VALUES ('normal','user','Read')"
                               ).lastrowid
        # A 1.6 attachment: a small PDF whose extracted text is far larger than the file, and an image.
        conn.execute("INSERT INTO chat_attachments(id,message_id,kind,filename,media_type,size_bytes,sha256,"
                     "extracted_text) VALUES ('pdf',?,'pdf','a.pdf','application/pdf',1500,'0',?)",
                     (message, "città " * 1000))
        conn.execute("INSERT INTO chat_attachments(id,message_id,kind,filename,media_type,size_bytes,sha256,"
                     "image_data) VALUES ('img',?,'image','a.png','image/png',4,'0',x'89504e47')", (message,))
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT session_id,user_id,is_incognito FROM active_streams ORDER BY session_id"
                            ).fetchall() == [("erased", None, 0), ("normal", "alice", 0), ("private", "alice", 1)]
        assert conn.execute("SELECT strikes,cooldown_until FROM worker_nodes").fetchone() == (0, None)
        assert conn.execute("SELECT requeues,deferred_by,requested_by,fault FROM worker_jobs").fetchone() == \
            (0, None, None, None)
        # The attachment limits count stored bytes: the extracted text in UTF-8, the stored image.
        assert conn.execute("SELECT id,size_bytes,stored_bytes FROM chat_attachments ORDER BY id").fetchall() == \
            [("img", 4, 4), ("pdf", 1500, len(("città " * 1000).encode()))]
        assert conn.execute("SELECT share_blocked_at FROM chat_sessions WHERE id='normal'").fetchone() == (None,)
        assert conn.execute("SELECT COUNT(*) FROM completion_leases").fetchone() == (0,)
        upgrade(conn)  # running it again changes nothing
        assert conn.execute("SELECT COUNT(*) FROM active_streams WHERE user_id='alice'").fetchone() == (2,)
        assert conn.execute("SELECT stored_bytes FROM chat_attachments WHERE id='img'").fetchone() == (4,)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
