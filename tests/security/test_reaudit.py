"""Regression tests for the BananaChat findings of the security re-audit (AUDIT.md, R4-*)."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

from tests.app.test_api import roll_out, token_for
from tests.app.test_images import artist, comfy, image_app  # noqa: F401
from tests.app.test_workers import register


def test_r4_1_idle_worker_polls_leave_room_for_streams(make_app, make_user, monkeypatch):
    from bananachat.services import remote

    remote._cooldown.clear()
    monkeypatch.setattr(remote, "LONG_POLL_SECONDS", 3)
    app = make_app(workers_enabled=1, worker_claim_timeout=5, http_threads=8)  # 4 long-request places
    roll_out(app)
    token = token_for(app, make_user("victim"))
    secrets = [register(app, name=f"PC{i}")[1] for i in range(4)]

    def poll(secret):
        app.test_client().get("/worker/v1/jobs/poll", headers={"Authorization": f"Bearer {secret}"})

    threads = [threading.Thread(target=poll, args=(secret,), daemon=True) for secret in secrets]
    for thread in threads:
        thread.start()
    time.sleep(0.5)

    assert app.extensions["bananachat.long_requests"].active <= 1
    response = app.test_client().post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                                      json={"model": "auto", "stream": True,
                                            "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    response.close()
    for thread in threads:
        thread.join(5)


def test_r4_3_an_upload_keeps_its_lease_while_it_sends_files(app, make_user, monkeypatch):
    from bananachat.db import agents as agents_db

    user = make_user("uploader")
    clock = [1000.0]
    monkeypatch.setattr(agents_db.time, "time", lambda: clock[0])
    with app.test_request_context():
        token = agents_db.claim_upload(user["id"])
        assert token
        for _ in range(5):  # five files, each taking the runner's longest transfer
            clock[0] += 330
            agents_db.renew_upload(user["id"], token)
            assert agents_db.claim_upload(user["id"]) is None
        agents_db.release_upload(user["id"], token)
        assert agents_db.claim_upload(user["id"])



def test_r4_4_lone_surrogates_are_refused_everywhere_not_crashing(app, make_user, client):
    import io

    from bananachat.services import personalities

    make_user("pp")
    client.login("pp")
    response = client.client.open("/api/preferences", method="POST", data=b'{"font": "x \\ud800"}', headers={
        "X-CSRF-Token": "test-csrf-token", "X-Requested-With": "fetch", "Accept": "application/json",
        "Content-Type": "application/json"})
    assert response.status_code == 400

    raw = ('{"format": "%s", "version": 1, "personality": {"name": "Bad \\ud800", "instructions": "x",'
           ' "description": "", "greeting": "", "starters": []}}' % personalities.EXPORT_FORMAT).encode()
    response = client.post("/personalities/import", {"file": (io.BytesIO(raw), "p.personality.json")},
                           content_type="multipart/form-data")
    assert response.status_code < 500
    assert client.get("/personalities").status_code == 200


def test_s2_1_one_person_holds_one_runner_transfer_of_any_kind(app, make_user, monkeypatch):
    from bananachat.db import agents as agents_db

    user = make_user("transfers")
    clock = [5000.0]
    monkeypatch.setattr(agents_db.time, "time", lambda: clock[0])
    with app.test_request_context():
        download = agents_db.claim_download(user["id"])
        assert download
        assert agents_db.claim_upload(user["id"]) is None  # not an upload beside a download
        clock[0] += 330  # an archive the runner builds for 300 s before the first byte
        assert agents_db.claim_download(user["id"]) is None
        agents_db.release_download(user["id"], download)
        assert agents_db.claim_upload(user["id"])


def _pdf_with_a_lone_surrogate() -> bytes:
    """A one-page PDF whose ToUnicode map turns glyph 0x41 into U+D800."""
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /X def "
            b"1 begincodespacerange <00> <FF> endcodespacerange "
            b"1 beginbfchar <41> <D800> endbfchar endcmap CMapName currentdict /CMap defineresource pop end end")
    content = b"BT /F1 24 Tf 72 700 Td (HelloA) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> "
        b"/Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def test_s2_2_a_pdf_with_a_lone_surrogate_does_not_crash_the_send(app, make_user):
    from bananachat.services import attachments

    with app.test_request_context():
        text = attachments._extract_pdf(_pdf_with_a_lone_surrogate(), 10_000, "report.pdf")
    assert "�" in text
    text.encode("utf-8")


def test_t2_2_background_uploads_take_an_image_decoding_place(app, make_user, client, monkeypatch):
    import io

    from PIL import Image

    from bananachat.services import attachments

    user = make_user("painter")
    client.login("painter")
    picture = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(picture, format="PNG")
    monkeypatch.setattr(attachments, "OWNER_DECODE_WAIT", 0.1)

    def upload():
        return client.client.post("/api/preferences/background", data={
            "file": (io.BytesIO(picture.getvalue()), "bg.png")}, headers={
            "X-CSRF-Token": "test-csrf-token", "X-Requested-With": "fetch", "Accept": "application/json"},
            content_type="multipart/form-data")

    client._ensure_csrf()
    with app.test_request_context(), attachments.decoding_image(f"user:{user['id']}"):
        busy = upload()
    assert busy.status_code == 503 and busy.headers["Retry-After"]
    assert upload().status_code == 200


def test_t2_2_pdf_extractions_are_bounded_per_process(monkeypatch):
    import pytest

    from bananachat.services import attachments

    monkeypatch.setattr(attachments, "IMAGE_DECODE_WAIT", 0.1)
    held = [attachments._pdf_extractions.acquire() for _ in range(attachments.IMAGE_DECODES)]
    try:
        with pytest.raises(attachments.AttachmentBusy):
            attachments._extract_pdf(b"%PDF-1.4\n", 100, "x.pdf")
    finally:
        for _ in held:
            attachments._pdf_extractions.release()


def test_u2_1_ids_beyond_sqlite_integers_answer_404_not_500(app, make_user, client):
    make_user("bigid")
    client.login("bigid")
    huge = str(2 ** 63)
    assert client.get(f"/personalities/{huge}/edit").status_code == 404
    assert client.post(f"/developer/tokens/{huge}/revoke").status_code == 404
    assert client.get(f"/personalities/{2 ** 63 - 1}/edit").status_code == 404  # the largest id still routes
    from bananachat.services import personalities

    with app.test_request_context():
        assert personalities.usable(make_user("other"), huge * 3) is None


def test_fc2_1_remote_access_polls_share_the_poll_places(make_app, make_user, monkeypatch):
    from bananachat.services import remote, worker_remote

    remote._cooldown.clear()
    monkeypatch.setattr(remote, "LONG_POLL_SECONDS", 3)
    monkeypatch.setattr(worker_remote, "LONG_POLL_SECONDS", 3)
    app = make_app(workers_enabled=1, worker_remote_access=1, worker_claim_timeout=5, http_threads=8)
    roll_out(app)
    token = token_for(app, make_user("victim"))
    headers = {"Authorization": f"Bearer {register(app, name='Busy PC')[1]}"}
    threads = [threading.Thread(target=lambda path=path: app.test_client().get(path, headers=headers), daemon=True)
               for path in ("/worker/v1/jobs/poll", "/worker/v1/remote/poll", "/worker/v1/remote/poll")]
    for thread in threads:
        thread.start()
    time.sleep(0.5)

    assert app.extensions["bananachat.long_requests"].active <= 1
    streams = [app.test_client().post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                                      json={"model": "auto", "stream": True,
                                            "messages": [{"role": "user", "content": "hi"}]}) for _ in range(2)]
    assert [response.status_code for response in streams] == [200, 200]
    for response in reversed(streams):  # the test client stacks their request contexts
        response.close()
    for thread in threads:
        thread.join(5)


def test_fc2_2_the_connect_helper_never_prints_a_workers_escape_sequences(capsys):
    import importlib.util
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "bananachat/static/downloads/bananachat_connect.py"
    spec = importlib.util.spec_from_file_location("bananachat_connect_under_test", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    hostile = "\x1b]52;c;cm0gLXJmIH4=\x07\x1b[2K\x1b[1Aconnected through Office PC"
    frames = [(helper.TEXT, json.dumps({"type": "error", "message": hostile}).encode()),
              (helper.TEXT, b"[]"), (helper.BINARY, b"data")]

    class FakeSocket:
        def receive(self):
            if not frames:
                raise EOFError
            return frames.pop(0)

        def send_binary(self, data):
            pass

        def close(self):
            pass

    written = []
    helper.relay(FakeSocket(), read=lambda: b"", write=written.append)

    printed = capsys.readouterr().err
    assert "\x1b" not in printed and "\x07" not in printed and "connected through Office PC" in printed
    assert written == [b"data"]  # a non-object message did not end the relay
    assert "\x1b" not in str(helper.Problem(hostile))


def _store_attachment_text(app, user_id, *, messages, text):
    import secrets as token_source

    from bananachat.db import chats

    with app.app_context():
        session_id = chats.create(user_id)
        for _ in range(messages):
            message_id = chats.add_message(session_id, "user", "see file", user_id=user_id, incognito=False)
            chats.add_attachments(message_id, [{"id": token_source.token_urlsafe(12), "kind": "text",
                                                "filename": "a.txt", "media_type": "text/plain",
                                                "size_bytes": len(text), "sha256": "0" * 64,
                                                "extracted_text": text}])


def test_fc1_1_exports_are_streamed_with_bounded_memory(app, make_user, monkeypatch):
    import json
    import tracemalloc

    from bananachat.db import users
    from bananachat.services import exports
    from tests.app.test_chat import user_browser

    monkeypatch.setattr(exports, "MESSAGE_BATCH", 4)
    monkeypatch.setattr(exports, "CONTENT_BATCH_BYTES", 256 * 1024)
    alice = user_browser(app, make_user)
    with app.app_context():
        user_id = users.get_by_username("alice")["id"]
    _store_attachment_text(app, user_id, messages=40, text="x" * 200_000)  # 8 MB of attachment text

    response = alice.post("/account/export/chats", buffered=False)
    assert response.status_code == 200 and response.is_streamed
    tracemalloc.start()
    try:
        size = sum(len(chunk) for chunk in response.response)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
        response.close()
    assert size > 8_000_000
    # The whole export was read into memory several times over before (3x the stored data).
    assert peak < 3_000_000, peak
    with app.app_context():
        data = json.loads("".join(exports.stream("chats", user_id)))
        assert data == {**exports.chats_export(user_id), "exported_at": data["exported_at"]}
    assert len(data["chats"][0]["messages"]) == 40


def test_fc1_1_one_export_per_account_at_a_time_and_only_by_post(app, make_user):
    from bananachat import db
    from tests.app.test_chat import user_browser

    alice = user_browser(app, make_user)
    bob = user_browser(app, make_user, "bob")
    first = alice.post("/account/export/gdpr", buffered=False)
    assert first.status_code == 200
    next(iter(first.response))  # still downloading
    busy = alice.post("/account/export/chats")
    assert busy.status_code == 429 and busy.headers["Retry-After"] == "30"
    assert bob.post("/account/export/chats").status_code == 200
    first.close()
    second = alice.post("/account/export/chats")
    assert second.status_code == 200
    second.close()

    # A link or a cross-site navigation (GET) starts nothing: it only leads to the account page.
    for url in ("/account/export/gdpr", "/account/export/chats"):
        response = alice.get(url)
        assert response.status_code == 302 and response.headers["Location"].endswith("/account#data")
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='account.export'", (), 0) == 3


def test_fc1_2_message_text_counts_towards_the_account_storage_limit(make_app, make_user):
    from bananachat.db import chats, users
    from tests.app.test_chat import new_chat, send, setup_models, user_browser, wait_idle

    app = make_app(BC_CHAT_MAX_ACCOUNT_TEXT_MB=1)
    setup_models(app)
    alice = user_browser(app, make_user)
    admin = user_browser(app, make_user, "root", role="admin")
    session_id = new_chat(alice)
    with app.app_context():
        alice_id = users.get_by_username("alice")["id"]
        # 0.6 MB in a chat and 0.4 MB of no-history copies kept for administrators (the chat itself is gone).
        chats.add_message(session_id, "user", "a" * 600_000, user_id=alice_id, incognito=False)
        hidden = chats.create(alice_id)
        chats.add_message(hidden, "user", "é" * 200_000, user_id=alice_id, incognito=True)
        chats.hard_delete(hidden)
        assert chats.account_text_bytes(alice_id) >= 1_000_000

    refused = send(alice, session_id, "x" * 60_000)
    assert refused.status_code == 413 and refused.get_json()["error"]["code"] == "storage_limit"
    assert "storage limit for messages (1 MB)" in refused.get_json()["error"]["message"]
    assert send(alice, session_id, "short").status_code == 200  # still under the limit
    wait_idle(app, session_id)
    # Administrators are exempt.
    root_chat = new_chat(admin)
    with app.app_context():
        root_id = users.get_by_username("root")["id"]
        chats.add_message(root_chat, "user", "r" * 1_100_000, user_id=root_id, incognito=False)
    assert send(admin, root_chat, "x" * 60_000).status_code == 200
    wait_idle(app, root_chat)


def _image_chat(app, browser, username, size=100_000):
    """A chat of *username* with one image attachment of *size* bytes: (chat id, attachment URL)."""
    import secrets as token_source

    from bananachat.db import chats, users
    from tests.app.test_chat import new_chat

    session_id = new_chat(browser)
    with app.app_context():
        user_id = users.get_by_username(username)["id"]
        message_id = chats.add_message(session_id, "user", "picture " * 20_000, user_id=user_id, incognito=False)
        attachment_id = token_source.token_urlsafe(12)
        chats.add_attachments(message_id, [{"id": attachment_id, "kind": "image", "filename": "a.png",
                                            "media_type": "image/png", "size_bytes": size, "sha256": "0" * 64,
                                            "image_data": b"\x89PNG" + b"0" * (size - 4)}])
    return session_id, f"/chat/{session_id}/attachments/{attachment_id}"


def _held(client, url):
    response = client.get(url, buffered=False)
    assert response.status_code == 200, (url, response.status_code)
    next(iter(response.response))  # a slow reader: the response stays open
    return response


def test_gc1_1_jc1_2_large_responses_take_download_places_not_the_threads_for_everything_else(make_app, make_user):
    from bananachat.db import chats
    from tests.app.test_chat import user_browser

    app = make_app(http_threads=8, download_threads=8)  # 8 download places, 2 per client
    alice, bob, carol, dave = (user_browser(app, make_user, name) for name in ("alice", "bob", "carol", "dave"))
    session_id, attachment = _image_chat(app, alice, "alice")
    with app.app_context():
        token = chats.share(session_id)
    held = [_held(alice, f"/chat/{session_id}/download"), _held(alice, attachment)]
    # One account holds at most a quarter of the places, whatever it downloads (files, exports, pages).
    refused = alice.get(f"/chat/{session_id}/download")
    assert refused.status_code == 429 and refused.headers["Retry-After"] == "5"
    visitor = app.test_client()
    held += [_held(visitor, f"/share/{token}/download"), _held(visitor, f"/share/{token}")]
    assert visitor.get(f"/share/{token}/download").status_code == 429
    for name, browser in (("bob", bob), ("carol", carol)):
        own, _file = _image_chat(app, browser, name)
        held += [_held(browser, f"/chat/{own}/download"), _held(browser, f"/chat/{own}/download")]
    downloads = app.extensions["bananachat.downloads"]
    assert downloads.active == 8
    late, _file = _image_chat(app, dave, "dave")
    assert dave.get(f"/chat/{late}/download").status_code == 503  # four clients filled the process
    # Answers, streams and short requests keep their own threads and places.
    assert app.extensions.get("bananachat.long_requests") is None or \
        app.extensions["bananachat.long_requests"].active == 0
    assert app.test_client().get("/health").status_code == 200
    for response in reversed(held):  # streams opened last are closed first (test client contexts)
        response.close()
    assert downloads.active == 0 and not downloads.clients
    finished = alice.get(f"/chat/{session_id}/download")
    assert "picture" in finished.get_data(as_text=True)
    finished.close()


def test_gc1_2_the_running_export_mark_is_renewed_while_the_download_goes_on(app, make_user, monkeypatch):
    from bananachat import db
    from bananachat.web import account
    from tests.app.test_chat import user_browser

    monkeypatch.setattr(account, "EXPORT_RENEW_SECONDS", 0)
    monkeypatch.setattr(account, "EXPORT_PIECE_CHARS", 64)
    alice = user_browser(app, make_user)
    first = alice.post("/account/export/gdpr", buffered=False)
    pieces = iter(first.response)
    next(pieces)
    # The download has been going on for longer than the mark's window...
    with app.app_context():
        db.execute("UPDATE rate_limit_hits SET hit_at=? WHERE key LIKE 'account-export-running:%'",
                   (db.now(-timedelta(seconds=account.EXPORT_RUNNING_SECONDS + 60)),))
    next(pieces)  # ...and the client is still reading: the mark counts again
    assert alice.post("/account/export/chats").status_code == 429
    first.close()
    second = alice.post("/account/export/chats")
    assert second.status_code == 200
    second.close()


def test_ic1_1_every_large_response_holds_a_place_until_it_is_closed(make_app, make_user):
    from tests.app.test_chat import user_browser

    app = make_app(download_threads=4)
    alice = user_browser(app, make_user)
    _session_id, attachment = _image_chat(app, alice, "alice")
    small = alice.get("/account")  # pages and small answers take no place
    assert small.status_code == 200
    first = _held(alice, attachment)  # one of 4 places per client: a quarter
    assert alice.get(attachment).status_code == 429
    first.close()
    assert app.extensions["bananachat.downloads"].active == 0
    head = alice.client.head(attachment)  # HEAD sends no body: no place
    assert head.status_code == 200 and app.extensions["bananachat.downloads"].active == 0


def test_ic1_1_files_sent_with_sendfile_give_their_places_back(app, make_user):
    from bananachat.db import users
    from tests.app.test_chat import user_browser

    alice = user_browser(app, make_user)
    name = "d" * 32 + ".jpg"
    path = app.config["BC"].background_upload_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"jpeg" * 20_000)  # 80 KB: a large response
    with app.app_context():
        users.save_preferences(users.get_by_username("alice")["id"], {"background_image": name})
    # send_from_directory hands the server the file wrapper itself; only its close() is called.
    for _ in range(10):
        response = alice.get(f"/customization/background/{name}")
        assert response.status_code == 200 and response.data == b"jpeg" * 20_000
        response.close()
    places = app.extensions["bananachat.downloads"]
    assert places.active == 0 and not places.clients


def test_jc1_1_chat_pages_stop_at_a_size_and_share_views_are_limited(make_app, make_user, monkeypatch):
    from bananachat.db import chats, users
    from bananachat.web import chat as chat_views
    from tests.app.test_chat import new_chat, user_browser

    monkeypatch.setattr(chat_views, "SHARED_VIEWS_PER_MINUTE", 3)
    app = make_app()
    alice = user_browser(app, make_user)
    session_id = new_chat(alice)
    with app.app_context():
        user_id = users.get_by_username("alice")["id"]
        for number in range(4):  # 2.4 MB of text, escaped five times larger in a page
            chats.add_message(session_id, "user", f"{number}" + "&" * 600_000, user_id=user_id, incognito=False)
        token = chats.share(session_id)
    page = alice.get(f"/chat/{session_id}/messages").get_json()
    assert [message["content"][0] for message in page["messages"]] == ["3"] and page["has_more"]
    assert len(alice.get(f"/chat/{session_id}").data) < 8 * 1024 * 1024
    visitor = app.test_client()
    for _ in range(3):
        shared = visitor.get(f"/share/{token}")
        assert shared.status_code == 200 and len(shared.data) < 8 * 1024 * 1024
        shared.close()
    limited = visitor.get(f"/share/{token}")
    assert limited.status_code == 429 and limited.headers["X-Robots-Tag"] == "noindex, nofollow"


def test_jc1_3_finished_connections_never_hold_the_accept_loop(tmp_path, monkeypatch):
    import runpy
    import socket

    from gunicorn import util

    monkeypatch.setenv("BC_INSTANCE_DIR", str(tmp_path))
    monkeypatch.setattr(util, "close_graceful", util.close_graceful)  # restored after the test
    settings = runpy.run_path("gunicorn.conf.py")
    # One request per connection: pipelined answers cannot pile up behind a slow reader.
    assert settings["keepalive"] == 0
    assert util.close_graceful is settings["close_without_waiting"]

    # Gunicorn 26 lingered up to 2 s on its accept loop for a client that never closes its side.
    server, client = socket.socketpair()
    client.sendall(b"unread request data")
    started = time.monotonic()
    util.close_graceful(server)
    assert time.monotonic() - started < 0.2
    assert client.recv(10) == b""  # the client sees the end of the response, not a reset
    client.close()


def test_jc1_3_responses_get_a_send_buffer_for_small_pages():
    import socket

    from bananachat import write_timeout

    listener = socket.create_server(("127.0.0.1", 0))
    peer = socket.create_connection(listener.getsockname())
    sock, _address = listener.accept()
    try:
        app = write_timeout.guard(lambda environ, start_response: [b"page"], 120)
        list(app({"gunicorn.socket": sock}, lambda *args: None))
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) >= write_timeout.SEND_BUFFER
    finally:
        for item in (sock, peer, listener):
            item.close()


def test_lc1_2_the_status_poll_stops_at_a_size(make_app, make_user):
    from bananachat.db import chats, users
    from tests.app.test_chat import new_chat, user_browser

    app = make_app()
    alice = user_browser(app, make_user)
    session_id = new_chat(alice)
    with app.app_context():
        user_id = users.get_by_username("alice")["id"]
        first = chats.add_message(session_id, "user", "start", user_id=user_id, incognito=False)
        for number in range(30):  # 3 MB of text after the first message
            chats.add_message(session_id, "user", f"{number:02d}" + "x" * 100_000, user_id=user_id, incognito=False)
    budget = app.config["BC"].chat_max_message_bytes + app.config["BC"].chat_max_response_bytes
    seen, after = [], first
    while True:
        status = alice.get(f"/chat/{session_id}/status?after={after}").get_json()
        if not status["messages"]:
            break
        assert sum(len(message["content"]) for message in status["messages"]) <= max(1024 * 1024, budget) + 100_000
        seen += [message["content"][:2] for message in status["messages"]]
        after = max(int(message["id"]) for message in status["messages"]) if "id" in status["messages"][0] else None
        if after is None:
            break
    assert seen[:3] == ["00", "01", "02"]


def test_tc1_1_chat_downloads_stop_once_the_link_or_the_session_ends(app, make_user, monkeypatch):
    from bananachat import db, security
    from bananachat.db import chats, users
    from bananachat.web import chat as chat_views
    from tests.app.test_chat import new_chat, user_browser

    monkeypatch.setattr(security, "DOWNLOAD_CHECK_SECONDS", 0)
    monkeypatch.setattr(chat_views, "PAGE_TEXT_BYTES", 1000)  # one message per batch
    alice = user_browser(app, make_user)
    session_id = new_chat(alice)
    with app.app_context():
        user_id = users.get_by_username("alice")["id"]
        for number in range(20):
            chats.add_message(session_id, "user", f"message {number:02d} " + "x" * 2000, user_id=user_id,
                              incognito=False)
        token = chats.share(session_id)

    def read_after(response, change):
        pieces = iter(response.response)
        first = next(pieces)
        change()
        text = (first + b"".join(pieces)).decode()
        response.close()
        return text

    def revoke():
        with app.app_context():
            chats.revoke_share(session_id)

    shared = app.test_client().get(f"/share/{token}/download", buffered=False)
    assert "message 19" not in read_after(shared, revoke)  # the link was revoked mid-download

    with app.app_context():
        token = chats.share(session_id)
    shared = app.test_client().get(f"/share/{token}/download", buffered=False)

    def suspend():
        with app.app_context():
            db.execute("UPDATE users SET suspended=1 WHERE id=?", (user_id,))

    assert "message 19" not in read_after(shared, suspend)
    with app.app_context():
        db.execute("UPDATE users SET suspended=0 WHERE id=?", (user_id,))

    own = alice.get(f"/chat/{session_id}/download", buffered=False)

    def sign_out_everywhere():
        with app.app_context():
            db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))

    assert "message 19" not in read_after(own, sign_out_everywhere)


def test_vc1_1_a_running_api_answer_stops_once_its_key_is_revoked(app, make_user, fake_ollama, monkeypatch):
    from bananachat.db import tokens
    from bananachat.services import supervisor
    from tests.app.test_completion_leases import _user

    monkeypatch.setattr(supervisor, "TICK", 0.1)
    monkeypatch.setattr(supervisor, "LEASE_INTERVAL", 0.2)
    user = _user(app, make_user, fake_ollama, words=1500, delay=0.005)
    raw = token_for(app, user)
    resp = app.test_client().post("/v1/chat/completions", headers={"Authorization": f"Bearer {raw}"}, buffered=False,
                                  json={"model": "auto", "stream": True,
                                        "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    stream = iter(resp.response)
    next(stream)
    with app.app_context():
        assert tokens.revoke(tokens.list_for(user["id"])[0]["id"], user["id"])
    began = time.monotonic()
    text = b"".join(stream).decode()
    assert time.monotonic() - began < 5
    assert "invalid_api_key" in text and text.count('"content"') < 1500


def test_vc1_1_a_running_image_stops_once_its_key_is_revoked(app, make_user):
    from bananachat.db import tokens
    from bananachat.services.images import ImageJob

    user = make_user("imager")
    token_for(app, user)
    job = ImageJob.__new__(ImageJob)
    with app.app_context():
        job.user, job.token_id = user, tokens.list_for(user["id"])[0]["id"]
        assert job._account_check(True) is None
        tokens.revoke(job.token_id, user["id"])
        assert job._account_check(True) == "token revoked"


def test_zc1_1_a_stolen_session_cannot_retire_the_owners_device_cookies(app, make_user):
    from tests.app.fixtures import Browser

    make_user("alice")
    owner = Browser(app)
    owner.login("alice")
    owner.logout()
    thief = Browser(app)
    thief.login("alice")  # stands in for a stolen session cookie
    assert thief.post("/account/sessions/revoke-others", {"revoke_api_tokens": "0"}).status_code == 302
    attacker = Browser(app)
    for _ in range(10):
        attacker.post("/login", {"username": "alice", "password": "wrong"})
    # The owner's browser keeps its own budget, signs in and can end the thief's session.
    assert owner.post("/login", {"username": "alice", "password": "alice-password"}).status_code == 302


def test_ac1_1_an_images_page_image_takes_its_download_place_before_it_is_made(image_app, artist, comfy):  # noqa: F811
    from bananachat import security
    from tests.app.fixtures import Browser
    from tests.app.test_images import MODEL_ID

    browser = Browser(image_app)
    browser.login("artist")
    browser._ensure_csrf()
    with image_app.app_context():
        places = security._downloads()
        held = [places.take("user:" + artist["id"])[0] for _ in range(places.per_client)]
    try:
        response = browser.post_json("/images/generate", {"prompt": "A cat", "model": MODEL_ID, "size": "512x512"})
        assert response.status_code == 429
        assert comfy.prompts == []  # refused before it was generated or charged
    finally:
        for release in held:
            release()
    assert browser.post_json("/images/generate", {"prompt": "A cat", "model": MODEL_ID,
                                                  "size": "512x512"}).status_code == 200
    assert places.active == 0


def test_ac1_2_share_links_made_from_a_stolen_session_can_be_stopped_with_it(app, make_user):
    from bananachat.db import users
    from tests.app.fixtures import Browser
    from tests.app.test_chat import new_chat, send, setup_models

    owner_user = make_user("alice")
    setup_models(app)
    thief, owner = Browser(app), Browser(app)
    thief.login("alice")
    owner.login("alice")
    chat_id = new_chat(thief)
    assert send(thief, chat_id).status_code == 200
    path = "/share/" + thief.post_json(f"/chat/{chat_id}/share").get_json()["url"].rsplit("/", 1)[1]
    thief.post_json(f"/chat/{chat_id}/share")  # the same link again: audited once
    with app.app_context():
        assert len(users.list_audit(action="chat.share_created")) == 1
    assert Browser(app).get(path).status_code == 200
    page = owner.get("/account").get_data(as_text=True)
    assert 'name="revoke_share_links"' in page
    assert owner.post("/account/sessions/revoke-others", {"revoke_share_links": "1"}).status_code == 302
    assert Browser(app).get(path).status_code == 404
    with app.app_context():
        assert [row["target"] for row in users.list_audit(action="account.share_links_revoke_all")] == [owner_user["id"]]


def test_bc1_1_share_link_audit_entries_have_a_budget_per_account(app, make_user):
    from bananachat.db import users
    from bananachat.web.chat import SHARE_AUDITS
    from tests.app.fixtures import Browser
    from tests.app.test_chat import new_chat, send, setup_models

    make_user("alice")
    setup_models(app)
    browser = Browser(app)
    browser.login("alice")
    chat_id = new_chat(browser)
    assert send(browser, chat_id).status_code == 200
    for _ in range(SHARE_AUDITS + 20):
        assert browser.post_json(f"/chat/{chat_id}/share").status_code == 200
        assert browser.post_json(f"/chat/{chat_id}/share", {"action": "revoke"}).status_code == 200
    with app.app_context():
        assert len(users.list_audit(action="chat.share_created")) == SHARE_AUDITS


def test_bc1_2_an_administrator_signing_a_compromised_account_out_can_stop_its_share_links(app, admin, make_user):
    from bananachat.db import users
    from tests.app.fixtures import Browser
    from tests.app.test_chat import new_chat, send, setup_models

    alice = make_user("alice")
    setup_models(app)
    thief = Browser(app)
    thief.login("alice")
    chat_id = new_chat(thief)
    assert send(thief, chat_id).status_code == 200
    path = "/share/" + thief.post_json(f"/chat/{chat_id}/share").get_json()["url"].rsplit("/", 1)[1]
    assert 'name="revoke_share_links"' in admin.get(f"/admin/users/{alice['id']}").get_data(as_text=True)
    assert admin.post(f"/admin/users/{alice['id']}/sessions/revoke-all",
                      {"revoke_share_links": "1"}).status_code == 302
    assert Browser(app).get(path).status_code == 404
    with app.app_context():
        assert len(users.list_audit(action="admin.user_share_links_revoke_all")) == 1


def test_dc1_1_a_non_number_of_hours_for_an_access_key_is_refused(app, admin):
    for hours in ([1], {"a": 1}, "x"):
        response = admin.post_json("/admin/workers/keys", {"hours": hours, "reauth_secret": "admin-password"})
        assert response.status_code == 400


def test_ec1_1_a_json_body_that_is_not_an_object_does_not_fail_the_worker_state_route(app, admin):
    for body in ([1], 5, True, "x"):
        response = admin.post_json("/admin/workers/abc/state", body)
        assert response.status_code == 404
