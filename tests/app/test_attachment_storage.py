"""Attachment storage limits count what is stored, and image decoding is shared fairly between accounts."""

from __future__ import annotations

import io
import threading
import time
import zlib

import pytest

from tests.app.test_chat import new_chat, send, setup_models, user_browser


def _compressed_pdf(lines: int = 1200) -> bytes:
    """A PDF of about 1.5 KB whose text extracts to about 200,000 characters (a Flate-compressed page)."""
    content = b"BT /F1 10 Tf 20 800 Td 12 TL " + b" ".join(
        [b"(" + b"The quick brown fox jumps over the lazy dog " * 4 + b") '"] * lines) + b" ET"
    stream = zlib.compress(content, 9)
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    output = io.BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, 1):
        offsets.append(output.tell())
        output.write(b"%d 0 obj\n" % index + body + b"\nendobj\n")
    xref = output.tell()
    output.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        output.write(b"%010d 00000 n \n" % offset)
    output.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return output.getvalue()


def test_a_small_pdf_counts_the_text_it_stores_against_the_account_limit(make_app, make_user, fake_ollama):
    from bananachat import db
    from bananachat.db import limits

    app = make_app(BC_CHAT_MAX_ACCOUNT_ATTACHMENT_MB=1)
    setup_models(app)
    with app.app_context():
        # Six sends in a row exceed the default chat burst (5, then 1 a second) on a fast machine; this test is
        # about storage, so the request rate must not answer first.
        policy = limits.get_policy("chat")
        policy["rate"]["enabled"] = False
        limits.set_policy("chat", policy, None)
    browser = user_browser(app, make_user, "hoarder")
    pdf = _compressed_pdf()
    assert len(pdf) < 4096
    accepted = 0
    for _ in range(8):
        response = send(browser, new_chat(browser), "Read this", files=[("tiny.pdf", pdf)])
        if response.status_code != 200:
            assert response.status_code == 413 and response.get_json()["error"]["code"] == "storage_limit"
            break
        accepted += 1
    # About 200 KB of text is stored per file: the 1 MB allowance holds five of them, not hundreds.
    assert accepted == 5, accepted
    with app.app_context():
        row = db.one("SELECT size_bytes, stored_bytes, length(extracted_text) AS chars FROM chat_attachments LIMIT 1")
    assert row["size_bytes"] == len(pdf)  # the file's own size, as the chat shows it
    assert row["stored_bytes"] >= row["chars"] > 150_000


def test_the_per_chat_limit_counts_stored_bytes(app, make_user):
    from bananachat.db import chats

    user = make_user("hoarder")
    with app.app_context():
        session_id = chats.create(user["id"])
        message_id = chats.add_message(session_id, "user", "Read", user_id=user["id"], incognito=False)
        chats.add_attachments(message_id, [{
            "id": "a" * 24, "kind": "pdf", "filename": "tiny.pdf", "media_type": "application/pdf",
            "size_bytes": 1500, "stored_bytes": 200_000, "sha256": "0" * 64, "extracted_text": "x" * 200_000,
            "image_data": None}])
        assert chats.attachment_bytes(session_id) == 200_000
        assert chats.account_attachment_bytes(user["id"]) == 200_000


def test_stored_size_counts_extracted_text_in_bytes_and_images_as_stored(app):
    from werkzeug.datastructures import FileStorage

    from bananachat.services import attachments

    config = app.config["BC"]
    text = "città è più " * 1000  # accented letters take two bytes each
    [item] = attachments.process([FileStorage(io.BytesIO(text.encode()), filename="notes.txt")], config)
    assert item["stored_bytes"] == len(item["extracted_text"].encode("utf-8")) == len(text.encode())
    [pdf] = attachments.process([FileStorage(io.BytesIO(_compressed_pdf()), filename="a.pdf")], config)
    assert pdf["stored_bytes"] == len(pdf["extracted_text"].encode("utf-8")) and pdf["size_bytes"] < 4096


# ----- image decoding ---------------------------------------------------------------------------------------------

def _png(width=64, height=64) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (width, height), (240, 200, 30)).save(output, format="PNG")
    return output.getvalue()


def _slow_decodes(monkeypatch, seconds: float, *, api: bool = False):
    """Make every image decode take *seconds* (chat uploads, or with *api* the API's check); returns the module and
    the peak number of decodes at once."""
    from PIL import Image

    from bananachat.services import attachments

    state = {"active": 0, "peak": 0}
    lock = threading.Lock()
    owner, name = (Image.Image, "load") if api else (attachments, "_reencode_image")
    original = getattr(owner, name)

    def slow(*args, **kwargs):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            time.sleep(seconds)
            return original(*args, **kwargs)
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(owner, name, slow)
    return attachments, state


def test_one_account_never_holds_every_image_decoding_place(app, monkeypatch):
    attachments, state = _slow_decodes(monkeypatch, 0.4)
    results = []

    def decode(owner):
        try:
            attachments._normalize_image(_png(), 1_000_000, "x.png", owner=owner)
            results.append((owner, "ok"))
        except attachments.AttachmentBusy:
            results.append((owner, "busy"))

    burst = [threading.Thread(target=decode, args=("user:mallory",)) for _ in range(6)]
    for thread in burst:
        thread.start()
    time.sleep(0.1)
    started = time.monotonic()
    decode("user:bob")  # another account is served at once, not after mallory's queue
    assert results[-1] == ("user:bob", "ok") and time.monotonic() - started < 1.5
    for thread in burst:
        thread.join(30)
    assert state["peak"] <= attachments.IMAGE_DECODES


def test_waiting_for_an_image_decoding_place_is_short(app, monkeypatch):
    attachments, _state = _slow_decodes(monkeypatch, 3 * attachments_owner_wait())
    outcomes = []

    def decode():
        started = time.monotonic()
        try:
            attachments._normalize_image(_png(), 1_000_000, "x.png", owner="user:mallory")
            outcomes.append(("ok", time.monotonic() - started))
        except attachments.AttachmentBusy:
            outcomes.append(("busy", time.monotonic() - started))

    threads = [threading.Thread(target=decode) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    # One of the account's requests decodes; the others give up quickly instead of holding a request thread.
    assert sorted(kind for kind, _ in outcomes) == ["busy", "busy", "ok"]
    assert all(seconds < attachments_owner_wait() + 2 for kind, seconds in outcomes if kind == "busy")


def attachments_owner_wait() -> float:
    from bananachat.services import attachments

    return attachments.OWNER_DECODE_WAIT


def test_api_images_are_decoded_one_request_at_a_time_per_account(app, make_user, monkeypatch):
    import base64

    from bananachat.services import completions

    attachments, state = _slow_decodes(monkeypatch, 0.3, api=True)
    image = "data:image/png;base64," + base64.b64encode(_png()).decode()
    body = {"model": "auto", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Look"}, {"type": "image_url", "image_url": {"url": image}}]}]}
    errors = []

    def parse():
        with app.test_request_context():
            from flask import g

            g.api_user = {"id": "mallory", "role": "user"}
            try:
                completions.parse_chat_request(body)
            except completions.CompletionError as error:
                errors.append(error)

    threads = [threading.Thread(target=parse) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert state["peak"] == 1  # one account: one decode at a time
    assert all(error.status == 503 and error.code == "busy" for error in errors)


@pytest.mark.parametrize("owner", [None, "user:x"])
def test_decoding_places_are_released(app, owner):
    from bananachat.services import attachments

    for _ in range(attachments.IMAGE_DECODES + 3):
        attachments._normalize_image(_png(), 1_000_000, "x.png", owner=owner)
    assert attachments._owners == set()
