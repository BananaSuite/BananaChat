"""Attachment storage per account, and the memory image uploads may take."""

from __future__ import annotations

import io
import threading
import time

from tests.app.test_chat import new_chat, send, setup_models, user_browser


def _text_file(size: int) -> bytes:
    return (b"banana " * (size // 7 + 1))[:size]


def test_an_account_cannot_store_more_attachments_than_its_limit(make_app, make_user, fake_ollama):
    app = make_app(BC_CHAT_MAX_ACCOUNT_ATTACHMENT_MB=1, BC_CHAT_MAX_EXTRACTED_CHARS=1_000_000)  # stored whole
    setup_models(app)
    browser = user_browser(app, make_user, "hoarder")
    first = new_chat(browser)
    assert send(browser, first, "Read this", files=[("a.txt", _text_file(600 * 1024))]).status_code == 200
    second = new_chat(browser)
    refused = send(browser, second, "And this", files=[("b.txt", _text_file(600 * 1024))])
    assert refused.status_code == 413, refused.get_data(as_text=True)
    body = refused.get_json()
    assert body["error"]["code"] == "storage_limit" and "1 MB" in body["error"]["message"]
    # Deleting a chat does not free its files until the chat is erased.
    assert browser.post(f"/chat/{first}/delete", {}, headers={"X-Requested-With": "fetch"}).status_code == 200
    assert send(browser, second, "And this", files=[("b.txt", _text_file(600 * 1024))]).status_code == 413
    from bananachat.db import chats

    with app.app_context():
        chats.purge_deleted("9999-01-01 00:00:00")
    assert send(browser, second, "And this", files=[("b.txt", _text_file(600 * 1024))]).status_code == 200
    # Messages without files are never refused.
    assert send(browser, new_chat(browser), "Just text").status_code == 200


def test_the_account_limit_is_translated_and_spares_administrators(make_app, make_user, fake_ollama):
    app = make_app(BC_CHAT_MAX_ACCOUNT_ATTACHMENT_MB=1, BC_CHAT_MAX_EXTRACTED_CHARS=1_000_000)  # stored whole
    setup_models(app)
    browser = user_browser(app, make_user, "hoarder")
    send(browser, new_chat(browser), "Read", files=[("a.txt", _text_file(900 * 1024))])
    refused = browser.post(f"/chat/{new_chat(browser)}/send",
                           {"content": "x", "files": [(io.BytesIO(_text_file(200 * 1024)), "b.txt")]},
                           headers={"X-Requested-With": "fetch", "Accept-Language": "it"})
    assert refused.status_code == 413 and "allegati" in refused.get_json()["error"]["message"]
    admin = user_browser(app, make_user, "boss", role="admin")
    for _ in range(2):
        assert send(admin, new_chat(admin), "Read", files=[("a.txt", _text_file(900 * 1024))]).status_code == 200


def _jpeg(width, height, orientation=None) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (width, height), (240, 200, 30))
    output = io.BytesIO()
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    image.save(output, format="JPEG", quality=90, exif=exif.tobytes())
    return output.getvalue()


def test_large_photos_are_decoded_reduced_and_stored_at_the_model_size(app, monkeypatch):
    from PIL import Image, JpegImagePlugin

    from bananachat.services import attachments

    decoded = []
    original_load = JpegImagePlugin.JpegImageFile.load

    def load(self):
        result = original_load(self)
        decoded.append(self.size)
        return result

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "load", load)
    # A 6000 x 4000 photo taken in portrait orientation (EXIF rotation).
    data, media_type = attachments._normalize_image(_jpeg(6000, 4000, orientation=6), 30_000_000, "photo.jpg",
                                                    max_side=1024)
    assert media_type == "image/jpeg"
    with Image.open(io.BytesIO(data)) as stored:
        assert stored.size == (683, 1024)  # rotated upright, longest side 1024
    assert decoded and max(decoded[0]) <= 1500  # decoded at 1/4 scale, never at full size
    # Small images keep their size.
    data, _ = attachments._normalize_image(_jpeg(800, 600), 30_000_000, "small.jpg", max_side=1024)
    with Image.open(io.BytesIO(data)) as stored:
        assert stored.size == (800, 600)


def test_a_process_decodes_only_a_few_images_at_once(app, monkeypatch):
    from PIL import ImageOps

    from bananachat.services import attachments

    active, peak, lock = [0], [0], threading.Lock()
    original = ImageOps.exif_transpose

    def slow_transpose(image, **kwargs):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.2)
        with lock:
            active[0] -= 1
        return original(image, **kwargs)

    monkeypatch.setattr(ImageOps, "exif_transpose", slow_transpose)
    raw = _jpeg(64, 64)
    threads = [threading.Thread(target=attachments._normalize_image, args=(raw, 1_000_000, "x.jpg"))
               for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert 1 <= peak[0] <= attachments.IMAGE_DECODES


def test_chat_images_are_stored_no_larger_than_the_configured_side(make_app, make_user, fake_ollama):
    from PIL import Image

    from bananachat import db

    app = make_app(BC_CHAT_MAX_IMAGE_DIMENSION=512)
    setup_models(app, vision=True)
    browser = user_browser(app, make_user, "photographer")
    assert send(browser, new_chat(browser), "Look", files=[("big.jpg", _jpeg(3000, 1500))]).status_code == 200
    with app.app_context():
        stored = db.scalar("SELECT image_data FROM chat_attachments")
    with Image.open(io.BytesIO(stored)) as image:
        assert image.size == (512, 256)
