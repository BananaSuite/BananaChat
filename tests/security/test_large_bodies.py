"""Large request bodies are read only for callers the view serves (AUDIT.md F-1)."""

import io

import pytest


def _count_spool(monkeypatch):
    from bananachat import app as app_mod

    spooled = {"bytes": 0, "files": 0}
    original = app_mod._Request._get_file_stream

    def counting(self, total_content_length, content_type, filename=None, content_length=None):
        stream = original(self, total_content_length, content_type, filename, content_length)
        spooled["files"] += 1
        write = stream.write

        def counted(data):
            spooled["bytes"] += len(data)
            return write(data)

        stream.write = counted
        return stream

    monkeypatch.setattr(app_mod._Request, "_get_file_stream", counting)
    return spooled


@pytest.mark.parametrize("url,field,size", [
    ("/admin/music/tracks", "track", 45 * 1024 * 1024),     # MAX_TRACK_BYTES 50 MiB
    ("/agents", "files", 18 * 1024 * 1024),                  # agents_max_upload_bytes 20 MiB (up to 200)
])
def test_f_1_anonymous_bodies_are_refused_before_they_are_read(app, monkeypatch, url, field, size):
    spooled = _count_spool(monkeypatch)
    anonymous = app.test_client()
    anonymous.post(url, data={field: (io.BytesIO(b"\0" * size), "x.bin")}, content_type="multipart/form-data")
    assert spooled["bytes"] < 2 * 1024 * 1024, "anonymous body spooled to a temporary file before CSRF refused it"


def test_f_1_a_plain_user_gets_no_large_body_on_administration_uploads(app, make_user, monkeypatch):
    from tests.app.fixtures import Browser

    make_user("carol")
    browser = Browser(app)
    browser.login("carol")
    spooled = _count_spool(monkeypatch)
    response = browser.post("/admin/music/tracks", data={"track": (io.BytesIO(b"\0" * (45 * 1024 * 1024)), "x.mp3")},
                            content_type="multipart/form-data")
    assert response.status_code == 413 and spooled["bytes"] < 2 * 1024 * 1024


def test_f_1_an_anonymous_form_field_is_not_buffered_at_the_views_size(app, monkeypatch):
    """max_form_memory_size is raised to the view's limit too: a plain field of that size is buffered in RAM."""
    from bananachat import security

    seen = {}
    original = security.check_csrf

    def watching():
        try:
            original()
        finally:
            from flask import request
            form = request.form
            seen["field"] = len(form.get("blob", ""))

    monkeypatch.setattr(security, "check_csrf", watching)
    anonymous = app.test_client()
    size = 45 * 1024 * 1024
    response = anonymous.post("/admin/music/tracks", data={"blob": "a" * size}, content_type="multipart/form-data")
    assert response.status_code == 413 and (seen.get("field") or 0) < 2 * 1024 * 1024


def test_g_1_a_user_without_cloud_sessions_gets_no_large_body_on_their_views(app, make_user, monkeypatch):
    from tests.app.fixtures import Browser

    make_user("dave")
    browser = Browser(app)
    browser.login("dave")
    spooled = _count_spool(monkeypatch)
    for url in ("/agents", "/agents/" + "0" * 32 + "/workspace/upload"):
        response = browser.client.post(url, data={"files": (io.BytesIO(b"\0" * (18 * 1024 * 1024)), "x.bin")},
                                       content_type="multipart/form-data")
        assert response.status_code == 413
    assert spooled["bytes"] < 2 * 1024 * 1024
