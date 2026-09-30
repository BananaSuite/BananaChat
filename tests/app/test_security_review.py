"""Regression tests for problems found in the security review."""

from __future__ import annotations

import io
import json
import struct
import time
import zlib

import pytest

from tests.app.test_workers import MODEL, Worker, generate, job_rows, register, run_in_thread


def _png_header(width: int, height: int) -> bytes:
    """A PNG that declares its size but carries (almost) no pixels: a decompression bomb's header."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b""))


def _image_body(data: bytes) -> dict:
    import base64

    url = "data:image/png;base64," + base64.b64encode(data).decode()
    return {"messages": [{"role": "user", "content": [{"type": "text", "text": "What is this?"},
                                                      {"type": "image_url", "image_url": {"url": url}}]}]}


@pytest.fixture
def pool_app(app, make_app):
    return make_app(workers_enabled=1, worker_claim_timeout=2)


# ----- workers ----------------------------------------------------------------------------------

def test_worker_cannot_charge_users_for_more_tokens_than_the_job_used(pool_app, make_user):
    _worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    worker.heartbeat()
    user = make_user("victim")

    def work():
        job = worker.poll().get_json()
        worker.chunk(job["job_id"], 0, "Hello")
        worker.chunk(job["job_id"], 1, "", done=True)
        assert worker.complete(job["job_id"], tokens_in=2**31 - 1, tokens_out=2**31 - 1).get_json()["ok"] is True

    thread, errors = run_in_thread(work)
    events = generate(pool_app, user)
    thread.join(10)
    assert not errors, errors

    from bananachat.services import remote
    finished = events[-1]
    assert finished.state == "completed"
    prompt = json.dumps([{"role": "user", "content": "Hi there"}], ensure_ascii=False)
    assert finished.prompt_tokens <= len(prompt.encode()) + remote.PROMPT_TOKEN_ALLOWANCE
    assert finished.completion_tokens == len("Hello") + remote.ANSWER_TOKEN_ALLOWANCE


def test_worker_failure_message_is_one_printable_line(pool_app):
    from bananachat.db import workers

    _worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    worker.heartbeat()
    with pool_app.app_context():
        workers.insert_job("a" * 32, MODEL, "[]", None, 1, time.time())
    job = worker.poll().get_json()
    worker.chunk(job["job_id"], 0, "partial")
    worker.fail(job["job_id"], {"error": "boom\n2026-01-01 ERROR bananachat: forged line\x1b[31m"})
    [row] = job_rows(pool_app)
    assert row["status"] == "failed"
    assert row["error_message"] == "boom 2026-01-01 ERROR bananachat: forged line [31m"


# ----- API images ---------------------------------------------------------------------------------

def test_api_refuses_images_with_too_many_pixels(app):
    from bananachat.services.completions import CompletionError, parse_chat_request

    config = app.config["BC"]
    with app.app_context():
        small = io.BytesIO()
        from PIL import Image
        Image.new("RGB", (2, 2)).save(small, format="PNG")
        assert parse_chat_request(_image_body(small.getvalue())).image_count == 1

        with pytest.raises(CompletionError) as caught:
            parse_chat_request(_image_body(_png_header(5000, 5000)))  # 25 MP > BC_CHAT_MAX_IMAGE_PIXELS
        assert caught.value.code == "image_too_large" and 5000 * 5000 > config.chat_max_image_pixels

        with pytest.raises(CompletionError) as caught:
            parse_chat_request(_image_body(_png_header(60000, 60000)))  # a decompression bomb
        assert caught.value.status == 400


# ----- background uploads ----------------------------------------------------------------------------

def test_background_upload_limit_applies_before_the_body_is_read(make_app):
    body = {"file": (io.BytesIO(b"x" * (5 * 1024 * 1024)), "big.png")}
    # Without a CSRF header the CSRF check reads the form: it must already be bounded by the configured size.
    response = make_app().test_client().post("/api/preferences/background", data=body,
                                             content_type="multipart/form-data")
    assert response.status_code == 413

    larger = make_app(background_image_max_mb=8).test_client()
    body = {"file": (io.BytesIO(b"x" * (5 * 1024 * 1024)), "big.png")}
    response = larger.post("/api/preferences/background", data=body, content_type="multipart/form-data")
    assert response.status_code == 400  # within the configured limit: refused by the CSRF check instead


# ----- PDF attachments -----------------------------------------------------------------------------

def _pdf(content: bytes, pages: int = 1) -> bytes:
    """A PDF whose pages share one Flate-compressed content stream."""
    stream = zlib.compress(content, 9)
    kids = " ".join(f"{4 + index} 0 R" for index in range(pages))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               f"<< /Type /Pages /Count {pages} /Kids [{kids}] >>".encode(),
               b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream"]
    objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 << /Type /Font "
                b"/Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents 3 0 R >>"] * pages
    output, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    output += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(output)


def test_pdf_extraction_is_bounded_in_time(monkeypatch):
    from bananachat.services import attachments

    assert "Quarterly" in attachments._extract_pdf(_pdf(b"BT /F1 12 Tf 20 100 Td (Quarterly) Tj ET"), 1000, "a.pdf")

    # A few kilobytes that decompress into millions of operators, on every page: pypdf would
    # spend many minutes on it inside the request thread.
    monkeypatch.setattr(attachments, "MAX_PDF_SECONDS", 2)
    bomb = _pdf(b"BT /F1 1 Tf " + b"0 0 Td " * 3_000_000 + b"ET", pages=50)
    assert len(bomb) < 64 * 1024
    started = time.monotonic()
    with pytest.raises(attachments.AttachmentError) as caught:
        attachments._extract_pdf(bomb, 1000, "bomb.pdf")
    assert caught.value.key == "chat.file_pdf_invalid"
    assert time.monotonic() - started < 10


# ----- invitations -------------------------------------------------------------------------------------

def test_administrator_invitations_stop_working_when_their_creator_is_no_longer_an_administrator(app, make_user):
    from bananachat.db import invites, users

    rogue = make_user("rogue", role="admin")
    with app.app_context():
        admin_code = invites.create(rogue["id"], max_uses=0, assigned_role="admin")
        user_code = invites.create(rogue["id"], max_uses=0, assigned_role="user")
        assert invites.find_usable(admin_code) and invites.find_usable(user_code)
        users.set_role(rogue["id"], "user")
        assert invites.find_usable(admin_code) is None
        assert invites.find_usable(user_code)
        users.set_role(rogue["id"], "admin")
        assert invites.find_usable(admin_code)
        users.delete(rogue["id"])
        assert invites.find_usable(admin_code) is None and invites.find_usable(user_code)


# ----- access log ---------------------------------------------------------------------------------------

def test_access_log_hides_share_tokens():
    pytest.importorskip("gunicorn")
    import runpy
    from datetime import timedelta
    from pathlib import Path
    from types import SimpleNamespace

    from gunicorn.config import Config

    settings = runpy.run_path(str(Path(__file__).resolve().parents[2] / "gunicorn.conf.py"))
    logger = settings["logger_class"](Config())
    response = SimpleNamespace(status="200 OK", headers=[], sent=10)

    def logged(path):
        environ = {"REQUEST_METHOD": "GET", "RAW_URI": path, "SERVER_PROTOCOL": "HTTP/1.1", "PATH_INFO": path}
        return logger.atoms(response, [], environ, timedelta(milliseconds=5))["U"]

    assert logged("/share/Abc-secret_token123456") == "/share/[token]"
    assert logged("/share/Abc-secret_token123456/download") == "/share/[token]/download"
    assert logged("/chat/abc") == "/chat/abc"
