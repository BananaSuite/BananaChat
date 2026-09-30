"""Music program: joining and leaving, the player, audio access and the admin page."""

from __future__ import annotations

import io

import pytest

from tests.app.conftest import TEST_CSRF, Browser

MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 256
OGG = b"OggS\x00\x02" + b"\x00" * 200


def _signed_in(app, username, password=None):
    browser = Browser(app)
    browser.login(username, password)
    with browser.client.session_transaction() as session:
        session["csrf"] = TEST_CSRF
    return browser


@pytest.fixture
def admin(app):
    """The administrator, with a CSRF token that survives rendering pages."""
    return _signed_in(app, "admin", "admin-password")


def _program(app, **values):
    from bananachat.db import settings

    defaults = {"music_enabled": 1, "music_visible": 1, "music_opt_in_allowed": 1, "music_opt_out_allowed": 1}
    with app.app_context():
        settings.update(**{**defaults, **values})


def _participation(app, user_id):
    from bananachat.db import music

    with app.app_context():
        return music.participation(user_id)


def _upload(admin, data, name, display_name=""):
    return admin.post("/admin/music/tracks", {"track": (io.BytesIO(data), name), "display_name": display_name})


def test_page_and_nav_follow_the_program_state(app, make_user):
    make_user("amy")
    browser = _signed_in(app, "amy")
    assert browser.get("/free-quota").status_code == 302  # disabled by default
    assert "/free-quota" not in browser.get("/account").get_data(as_text=True)
    _program(app, music_bonus_mode="fixed", music_bonus_fixed_tokens=25_000, music_bonus_fixed_slow_tokens=5000)
    html = browser.get("/free-quota").get_data(as_text=True)
    assert "+25k" in html and "55k" in html  # 30k tokens + 25k
    assert 'href="/free-quota"' in browser.get("/account").get_data(as_text=True)


def test_multiplier_bonus_is_explained(app, make_user):
    make_user("bo")
    _program(app, music_bonus_mode="multiplier", music_credit_multiplier=3)
    html = _signed_in(app, "bo").get("/free-quota").get_data(as_text=True)
    assert "×3" in html and "90" in html and "45" in html


def test_join_and_leave_respect_the_rules(app, make_user):
    user = make_user("cy")
    _program(app, music_opt_in_allowed=0)
    browser = _signed_in(app, "cy")
    browser.post("/free-quota/opt-in")
    assert _participation(app, user["id"]) == (False, False)
    _program(app)
    browser.post("/free-quota/opt-in")
    assert _participation(app, user["id"]) == (True, False)

    _program(app, music_opt_out_allowed=0)
    browser.post("/free-quota/opt-out")
    assert _participation(app, user["id"]) == (True, False)
    _program(app)
    browser.post("/free-quota/opt-out")
    assert _participation(app, user["id"]) == (False, False)


def test_forced_participants_cannot_leave_and_hidden_program_stays_reachable(app, make_user):
    from bananachat.db import music

    user = make_user("dee")
    _program(app)
    with app.app_context():
        music.set_participation(user["id"], True, forced=True)
    browser = _signed_in(app, "dee")
    browser.post("/free-quota/opt-out")
    assert _participation(app, user["id"]) == (True, True)

    _program(app, music_visible=0)
    assert browser.get("/free-quota").status_code == 200  # participants can still reach it
    make_user("eli")
    outsider = _signed_in(app, "eli")
    assert outsider.get("/free-quota").status_code == 302
    outsider.post("/free-quota/opt-in")
    with app.app_context():
        assert music.count_participants() == (1, 1)


def test_player_is_shown_to_participants_only(app, make_user, admin):
    from bananachat.db import music

    user = make_user("fox")
    make_user("gia")
    _program(app)
    assert _upload(admin, MP3, "song.mp3", "First song").status_code == 302
    browser = _signed_in(app, "fox")
    assert 'id="music-player"' not in browser.get("/account").get_data(as_text=True)
    with app.app_context():
        music.set_participation(user["id"], True)
    html = browser.get("/account").get_data(as_text=True)
    assert 'id="music-player"' in html and "First song" in html and "js/music-player.js" in html
    assert "autoplay" not in html
    _program(app, music_enabled=0)
    assert 'id="music-player"' not in browser.get("/account").get_data(as_text=True)


def test_audio_is_served_to_participants_and_admins_only(app, make_user, admin):
    from bananachat.db import music

    user = make_user("hal")
    make_user("ivy")
    _program(app)
    _upload(admin, MP3, "song.mp3")
    with app.app_context():
        filename = music.list_tracks()[0]["filename"]
        music.set_participation(user["id"], True)
    url = f"/music/audio/{filename}"

    response = admin.get(url)
    assert response.status_code == 200 and response.mimetype == "audio/mpeg"
    listener = _signed_in(app, "hal")
    partial = listener.get(url, headers={"Range": "bytes=0-9"})
    assert partial.status_code == 206 and partial.data == MP3[:10]
    assert "private" in partial.headers["Cache-Control"]
    assert _signed_in(app, "ivy").get(url).status_code == 404
    assert Browser(app).get(url).status_code == 302
    assert listener.get("/music/audio/../bananachat.db").status_code == 404
    assert listener.get("/music/audio/" + "0" * 32 + ".mp3").status_code == 404


def test_track_upload_validation_and_management(app, admin):
    from bananachat.db import music

    audio_dir = app.config["BC"].audio_dir
    assert _upload(admin, b"MZ\x90\x00 not audio", "evil.mp3").status_code == 302
    assert _upload(admin, OGG, "mismatch.mp3").status_code == 302
    assert _upload(admin, MP3, "script.exe").status_code == 302
    with app.app_context():
        assert music.list_tracks() == []
    _upload(admin, MP3, "one.mp3")
    _upload(admin, OGG, "two.ogg", "Second")
    with app.app_context():
        tracks = music.list_tracks()
    assert [track["display_name"] for track in tracks] == ["one", "Second"]
    assert all((audio_dir / track["filename"]).exists() for track in tracks)
    assert tracks[1]["filename"].endswith(".ogg")

    first, second = tracks
    admin.post(f"/admin/music/tracks/{second['id']}/move", {"direction": "up"})
    admin.post(f"/admin/music/tracks/{first['id']}/rename", {"display_name": "  Renamed   track "})
    with app.app_context():
        tracks = music.list_tracks()
    assert [track["display_name"] for track in tracks] == ["Second", "Renamed track"]

    admin.post(f"/admin/music/tracks/{first['id']}/delete")
    with app.app_context():
        assert [track["id"] for track in music.list_tracks()] == [second["id"]]
    assert not (audio_dir / first["filename"]).exists()
    page = admin.get("/admin/music")
    assert page.status_code == 200 and "Second" in page.get_data(as_text=True)


def test_admin_settings_allow_a_zero_bonus(app, admin, make_user):
    from bananachat.db import credits, music, settings

    user = make_user("jo")
    form = {"music_enabled": "1", "music_visible": "1", "music_opt_in_allowed": "1", "music_opt_out_allowed": "1",
            "music_bonus_mode": "fixed", "music_credit_multiplier": "2", "music_bonus_fixed_tokens": "0",
            "music_bonus_fixed_slow_tokens": "0", "music_playback_mode": "shuffle"}
    admin.post("/admin/music/settings", form)
    with app.app_context():
        current = settings.get()
        assert current["music_bonus_mode"] == "fixed" and current["music_bonus_fixed_tokens"] == 0
        assert current["music_playback_mode"] == "shuffle"
        music.set_participation(user["id"], True)
        assert credits.music_bonus(user["id"]) == ("fixed", 0.0, 0.0)
        assert users_audit_actions() >= {"music.settings"}
    admin.post("/admin/music/settings", {**form, "music_bonus_mode": "multiplier", "music_credit_multiplier": "11"})
    with app.app_context():
        assert settings.get()["music_bonus_mode"] == "fixed"  # rejected, nothing changed


def users_audit_actions():
    from bananachat.db import users

    return {row["action"] for row in users.list_audit()}


def test_participants_admin(app, admin, make_user):
    from bananachat.db import music, settings

    ids = [make_user(f"user{index:03d}")["id"] for index in range(60)]
    _program(app)
    html = admin.get("/admin/music?status=all").get_data(as_text=True)
    assert "Page 1 of 2" in html
    assert "user059" in admin.get("/admin/music?page=2").get_data(as_text=True)
    assert "user007" in admin.get("/admin/music?q=user007").get_data(as_text=True)

    admin.post(f"/admin/music/users/{ids[0]}/force-in")
    assert _participation(app, ids[0]) == (True, True)
    admin.post(f"/admin/music/users/{ids[0]}/release")
    assert _participation(app, ids[0]) == (True, False)
    admin.post(f"/admin/music/users/{ids[0]}/force-out")
    assert _participation(app, ids[0]) == (False, False)

    admin.post("/admin/music/opt-in-all")
    with app.app_context():
        assert music.count_participants() == (60, 0)  # regular accounts only
    admin.post("/admin/music/opt-out-all")
    with app.app_context():
        assert music.count_participants() == (0, 0)

    admin.post(f"/admin/music/users/{ids[1]}/force-in")
    admin.post("/admin/music/hide")
    with app.app_context():
        assert settings.get()["music_visible"] == 0
    admin.post("/admin/music/remove")
    with app.app_context():
        assert settings.get()["music_enabled"] == 0
        assert music.count_participants() == (0, 0)


def test_admin_music_requires_an_administrator(app, make_user):
    make_user("kim")
    assert _signed_in(app, "kim").get("/admin/music").status_code == 403
