"""Version 16 keeps effective allowances and history while retiring the second budget."""

from contextlib import closing
import json
import sqlite3

import pytest

from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION
from bananachat.db.migrations.v16_single_token_budget import upgrade
from sqlite_migrations import apply_migrations


@pytest.fixture
def old_db(tmp_path):
    with closing(sqlite3.connect(tmp_path / "v15.db")) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:15])
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT INTO users (id, username, password) VALUES ('u', 'uma', 'fixture')")
        yield conn


def _policy(conn, pool="api", **window):
    stored = json.loads(conn.execute("SELECT config FROM limit_policy WHERE pool=?", (pool,)).fetchone()[0])
    stored["window"].update(window)
    conn.execute("UPDATE limit_policy SET config=? WHERE pool=?", (json.dumps(stored), pool))
    return stored


def _upgraded_policy(conn, pool="api"):
    return json.loads(conn.execute("SELECT config FROM limit_policy WHERE pool=?", (pool,)).fetchone()[0])


def _migrate(conn):
    apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_active_allowances_combine_without_altering_other_policy_sections(old_db):
    before = _policy(old_db, tokens=123_456, slow_tokens=654_321, dynamic=True, auto_tiers=True)
    before["rate"] = {"enabled": True, "dynamic": True,
                      "rules": [{"requests": 17, "burst": 3, "per": "minute"}]}
    before["weekly"].update(enabled=True, tokens=765_432, dynamic=True, auto_tiers=True)
    old_db.execute("UPDATE limit_policy SET config=? WHERE pool='api'", (json.dumps(before),))
    _policy(old_db, "chat", tokens=50_000, slow_tokens=5000, enabled=True)
    _policy(old_db, "agent", tokens=60_000, slow_tokens=7000)
    _migrate(old_db)
    after = _upgraded_policy(old_db)
    assert after["version"] == 3
    assert after["window"] == {key: value for key, value in
                                {**before["window"], "tokens": 777_777}.items() if key != "slow_tokens"}
    assert after["rate"] == before["rate"]
    assert after["weekly"] == before["weekly"]
    assert _upgraded_policy(old_db, "chat")["window"]["tokens"] == 55_000
    assert _upgraded_policy(old_db, "agent")["window"]["tokens"] == 67_000


@pytest.mark.parametrize("regular,slow,automatic,expected,floor", [
    (40_000, 10_000, None, 50_000, False),
    (40_000, None, None, 70_000, False),
    (None, 10_000, None, 70_000, False),
    (None, None, None, None, False),
    (40_000, 10_000, "window_tokens,window_slow_tokens", 90_000, True),
    (40_000, 10_000, "window_tokens", 70_000, False),
    (None, 10_000, "window_slow_tokens", 90_000, True),
    (40_000, None, "window_tokens", 90_000, True),
    (0, 0, None, 0, False),
])
def test_override_null_inheritance_current_tier_and_automatic_floors(old_db, regular, slow, automatic,
                                                                  expected, floor):
    _policy(old_db, auto_tiers=True)
    tier = old_db.execute("SELECT id FROM limit_tiers WHERE multiplier=2").fetchone()[0]
    old_db.execute("INSERT INTO user_limits (user_id, tier_id, speed, dynamic_exempt, tier_locked) "
                   "VALUES ('u', ?, 'slow', 1, 1)", (tier,))
    old_db.execute("INSERT INTO user_limit_overrides (user_id,pool,window_tokens,window_slow_tokens,weekly_tokens, "
                   "rate_rules,automatic) VALUES ('u','api',?,?,234567,?,?)",
                   (regular, slow, '[{"requests":8,"per":"minute","burst":4}]', automatic))
    old_db.execute("INSERT INTO user_model_limits (user_id,model_id,locked) "
                   "SELECT 'u',id,1 FROM ai_models")
    _migrate(old_db)
    row = old_db.execute("SELECT * FROM user_limit_overrides WHERE user_id='u'").fetchone()
    assert row["window_tokens"] == expected
    assert row["window_slow_tokens"] == row["daily_slow_credits"] == 0
    assert row["weekly_tokens"] == 234_567
    assert json.loads(row["rate_rules"])[0]["requests"] == 8
    assert row["automatic"] == ("window_tokens" if floor else None)
    prefs = old_db.execute("SELECT speed,dynamic_exempt,tier_locked FROM user_limits WHERE user_id='u'").fetchone()
    assert tuple(prefs) == ("slow", 1, 1)


@pytest.mark.parametrize("regular,slow", [(1, None), (None, 1)])
def test_fractional_tier_inheritance_matches_legacy_whole_tokens(old_db, regular, slow):
    _policy(old_db, tokens=1, slow_tokens=1, auto_tiers=True)
    old_db.execute("UPDATE limit_tiers SET multiplier=1.25")
    old_db.execute("INSERT INTO user_limit_overrides (user_id,pool,window_tokens,window_slow_tokens) "
                   "VALUES ('u','api',?,?)", (regular, slow))
    _migrate(old_db)
    assert old_db.execute("SELECT window_tokens FROM user_limit_overrides").fetchone()[0] == 2


def test_disabled_slow_allowance_never_increases_regular_quota_or_bonus(old_db):
    old_db.execute("UPDATE site_settings SET slow_credits_enabled=0,quota_auto_approve_max_tokens=60000, "
                   "quota_auto_approve_max_slow_tokens=50000,music_bonus_fixed_tokens=10000, "
                   "music_bonus_fixed_slow_tokens=5000")
    old_db.execute("INSERT INTO user_limit_overrides (user_id,pool,window_tokens,window_slow_tokens,weekly_tokens) "
                   "VALUES ('u','api',40000,10000,200000),('u','chat',NULL,10000,200000)")
    old_db.execute("INSERT INTO quota_requests (user_id,kind,new_credits,new_slow_credits,new_tokens,new_slow_tokens) "
                   "VALUES ('u','window',50,20,50000,20000)")
    _migrate(old_db)
    assert _upgraded_policy(old_db)["window"]["tokens"] == 30_000
    rows = old_db.execute("SELECT pool,window_tokens,window_slow_tokens FROM user_limit_overrides ORDER BY pool").fetchall()
    assert [tuple(row) for row in rows] == [("api", 40_000, 0), ("chat", None, 0)]
    settings = old_db.execute("SELECT * FROM site_settings").fetchone()
    assert settings["quota_auto_approve_max_tokens"] == 60_000
    assert settings["music_bonus_fixed_tokens"] == 10_000
    assert settings["music_bonus_fixed_weekly_tokens"] == 70_000
    request = old_db.execute("SELECT new_tokens,new_slow_tokens FROM quota_requests").fetchone()
    assert tuple(request) == (50_000, 0)


@pytest.mark.parametrize("status", ["pending", "approved", "denied", "cancelled"])
def test_requests_combine_token_and_legacy_credit_fallbacks_keep_state_and_votes(old_db, status):
    old_db.execute("INSERT INTO quota_requests (user_id,kind,new_credits,new_slow_credits,new_tokens,new_slow_tokens, "
                   "status,community,community_until,admin_message,resolution_source) "
                   "VALUES ('u','window',80,30,NULL,12000,?,1,'2026-11-01 00:00:00','kept','community')", (status,))
    request_id = old_db.execute("SELECT id FROM quota_requests").fetchone()[0]
    old_db.execute("INSERT INTO quota_request_votes (request_id,user_id,stance,tokens,pool,scope) "
                   "VALUES (?,'u','support',9000,'api','window')", (request_id,))
    votes_before = [tuple(row) for row in old_db.execute("SELECT * FROM quota_request_votes")]
    _migrate(old_db)
    row = old_db.execute("SELECT * FROM quota_requests").fetchone()
    assert row["new_tokens"] == 92_000  # token amount wins over its stale 30-credit fallback
    assert row["new_credits"] == 92
    assert row["new_slow_tokens"] == row["new_slow_credits"] == 0
    assert (row["status"], row["community"], row["admin_message"], row["resolution_source"]) == (status, 1, "kept", "community")
    assert [tuple(row) for row in old_db.execute("SELECT * FROM quota_request_votes")] == votes_before


def test_fixed_music_weekly_bonus_and_unrelated_site_settings_are_preserved(old_db):
    old_db.execute("UPDATE site_settings SET music_bonus_fixed_tokens=NULL,music_bonus_fixed_credits=10, "
                   "music_bonus_fixed_slow_tokens=NULL,music_bonus_fixed_slow=5, "
                   "quota_auto_approve_max_tokens=70000,quota_auto_approve_max_slow_tokens=30000, "
                   "quota_auto_approve_max_weekly_tokens=250000,chat_local_token_consumption=1, "
                   "chat_cloud_token_consumption=0,effort_default_level='high',community_hours=123")
    _migrate(old_db)
    row = old_db.execute("SELECT * FROM site_settings").fetchone()
    assert row["music_bonus_fixed_tokens"] == 15_000
    assert row["music_bonus_fixed_weekly_tokens"] == 70_000
    assert row["quota_auto_approve_max_tokens"] == 100_000
    assert row["quota_auto_approve_max_weekly_tokens"] == 250_000
    assert (row["chat_local_token_consumption"], row["chat_cloud_token_consumption"]) == (1, 0)
    assert (row["effort_default_level"], row["community_hours"]) == ("high", 123)
    for name in ("slow_credits_enabled", "default_slow_credits", "chat_daily_slow_credits",
                 "quota_auto_approve_max_slow_tokens", "quota_auto_approve_max_slow_credits",
                 "music_bonus_fixed_slow_tokens", "music_bonus_fixed_slow"):
        assert row[name] == 0


def test_capped_conversion_keeps_history_schema_and_is_idempotent(old_db):
    _policy(old_db, tokens=900_000_000, slow_tokens=900_000_000)
    old_db.execute("INSERT INTO user_limit_overrides (user_id,pool,window_tokens,window_slow_tokens,automatic) "
                   "VALUES ('u','api',900000000,900000000,'window_tokens,window_slow_tokens,weekly_tokens')")
    old_db.execute("INSERT INTO credit_ledger (user_id,credits_used,is_slow,tokens_in,tokens_out) "
                   "VALUES ('u',12.5,1,12000,500),('u',3,0,2000,1000)")
    old_db.execute("INSERT INTO image_credit_reservations (user_id,credits_reserved,is_slow) VALUES ('u',7.5,1)")
    history = {table: [tuple(row) for row in old_db.execute(f'SELECT * FROM "{table}"')]
               for table in ("credit_ledger", "image_credit_reservations", "limit_tiers")}
    columns = {row["name"] for row in old_db.execute("PRAGMA table_info(site_settings)")}
    _migrate(old_db)
    assert _upgraded_policy(old_db)["window"]["tokens"] == 1_800_000_000
    assert old_db.execute("SELECT window_tokens FROM user_limit_overrides").fetchone()[0] == 1_800_000_000
    assert {row["name"] for row in old_db.execute("PRAGMA table_info(site_settings)")} == columns | {
        "music_bonus_fixed_weekly_tokens", "worker_offline_warning_enabled",
    }
    for table, rows in history.items():
        assert [tuple(row) for row in old_db.execute(f'SELECT * FROM "{table}"')] == rows
    before = list(old_db.iterdump())
    upgrade(old_db)
    assert list(old_db.iterdump()) == before


@pytest.mark.parametrize("regular,slow,automatic,expected", [
    (20_000_000, None, None, 4_020_000_000),
    (None, 20_000_000, None, 8_020_000_000),
    (20_000_000, 20_000_000, "window_tokens,window_slow_tokens", 12_000_000_000),
])
def test_large_inherited_tier_allowance_is_preserved(old_db, regular, slow, automatic, expected):
    _policy(old_db, tokens=800_000_000, slow_tokens=400_000_000, auto_tiers=True)
    old_db.execute("UPDATE limit_tiers SET multiplier=10")
    old_db.execute("INSERT INTO user_limit_overrides (user_id,pool,window_tokens,window_slow_tokens,automatic) "
                   "VALUES ('u','api',?,?,?)", (regular, slow, automatic))
    _migrate(old_db)
    assert _upgraded_policy(old_db)["window"]["tokens"] == 1_200_000_000
    assert old_db.execute("SELECT window_tokens FROM user_limit_overrides").fetchone()[0] == expected
    before = list(old_db.iterdump())
    upgrade(old_db)
    assert list(old_db.iterdump()) == before


def test_largest_supported_legacy_pool_allowances_keep_their_two_billion_total(old_db):
    _policy(old_db, tokens=1_000_000_000, slow_tokens=1_000_000_000)
    _migrate(old_db)
    assert _upgraded_policy(old_db)["window"]["tokens"] == 2_000_000_000


def test_fresh_database_has_one_45k_allowance_and_original_weekly_music_bonus(tmp_path):
    with closing(sqlite3.connect(tmp_path / "new.db")) as conn:
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        policy = _upgraded_policy(conn)
        assert policy["window"]["tokens"] == 45_000
        assert "slow_tokens" not in policy["window"]
        assert conn.execute("SELECT music_bonus_fixed_tokens,music_bonus_fixed_weekly_tokens,slow_credits_enabled "
                            "FROM site_settings").fetchone() == (45_000, 210_000, 0)
