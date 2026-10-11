"""A model is only marked failing for failures that are its own, never for what a user sent."""

from __future__ import annotations

import base64
import io
import struct
import zlib

import pytest

from tests.app.test_api import chat as api_chat
from tests.app.test_api import token_for
from tests.app.test_chat import setup_models


def _png(width=64, height=64) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (width, height), (200, 180, 20)).save(output, format="PNG")
    return output.getvalue()


def _corrupt_pixels(png: bytes) -> bytes:
    """The same PNG with undecodable image data: its header (and every chunk checksum) is still valid."""
    result, position = bytearray(png[:8]), 8
    while position < len(png):
        length, kind = struct.unpack(">I4s", png[position:position + 8])
        data = png[position + 8:position + 8 + length]
        if kind == b"IDAT":
            data = b"\x78\x9c" + bytes(range(256)) * 4  # a zlib header followed by garbage
        result += struct.pack(">I4s", len(data), kind) + data + struct.pack(">I", zlib.crc32(kind + data))
        position += 12 + length
    return bytes(result)


def _image_message(png: bytes) -> list:
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    return [{"role": "user", "content": [{"type": "text", "text": "What is this?"},
                                         {"type": "image_url", "image_url": {"url": url}}]}]


def _generate(app, name, user, *, images=None, fallbacks=()):
    from bananachat.db import catalog
    from bananachat.services import inference
    from bananachat.services.upstream import CancelToken

    message = {"role": "user", "content": "Hi"}
    if images:
        message["images"] = images
    with app.app_context():
        request = inference.TextRequest(user=user, model=catalog.get_by_name(name), messages=[message],
                                        options={"num_predict": 8},
                                        fallbacks=[catalog.get_by_name(other) for other in fallbacks])
        return list(inference.generate(request, CancelToken()))


def _failures(app, name):
    from bananachat.db import catalog

    with app.app_context():
        row = catalog.get_by_name(name)
        return row["recent_failures"], row["failing_at"]


def test_the_api_refuses_an_image_whose_pixels_cannot_be_decoded(app, make_user, fake_ollama):
    setup_models(app, vision=True)
    token = token_for(app, make_user("mallory"))
    broken = _corrupt_pixels(_png())
    from PIL import Image

    with Image.open(io.BytesIO(broken)) as image:
        assert image.size == (64, 64)  # the header alone looks fine
    response = api_chat(app, token, messages=_image_message(broken))
    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["error"]["code"] == "invalid_image"
    assert not [path for path, _ in fake_ollama.requests if path == "/api/chat"]
    # A whole image still works.
    assert api_chat(app, token, messages=_image_message(_png())).status_code == 200


def test_requests_the_model_server_refuses_do_not_count_against_the_model(app, make_user, fake_ollama, monkeypatch):
    from bananachat.services import ollama
    from bananachat.services.upstream import UpstreamError

    setup_models(app)
    user = make_user("mallory")

    def refuse(*args, **kwargs):
        raise UpstreamError("invalid request: input too long", 400)

    monkeypatch.setattr(ollama, "chat_stream", refuse)
    for _ in range(6):
        assert _generate(app, "qwen3:4b", user)[-1].state == "failed"
    assert _failures(app, "qwen3:4b") == (0, None)


def test_an_image_the_model_cannot_read_does_not_count_against_the_model(app, make_user, fake_ollama):
    setup_models(app, vision=True)
    user = make_user("mallory")
    fake_ollama.chat_records = [{"error": "failed to process inputs: image: unknown format"}]
    image = base64.b64encode(_png()).decode()
    for _ in range(6):
        assert _generate(app, "qwen3:4b", user, images=[image])[-1].state == "failed"
    assert _failures(app, "qwen3:4b") == (0, None)
    # The same error without anything the user sent is the model's own problem.
    fake_ollama.chat_records = [{"error": "llama runner process has terminated: exit status 2"}]
    _generate(app, "qwen3:4b", user)
    assert _failures(app, "qwen3:4b")[0] == 1


def test_fallback_attempts_after_a_failure_are_not_counted(app, make_user, fake_ollama):
    """One request trying every model in turn must not count against all of them."""
    setup_models(app, vision=True)
    user = make_user("mallory")
    fake_ollama.fail_models = {"qwen3:4b", "llama3.2:3b"}
    for _ in range(6):
        events = _generate(app, "qwen3:4b", user, fallbacks=["llama3.2:3b"])
        assert events[-1].state == "failed"
    assert _failures(app, "llama3.2:3b") == (0, None)
    assert _failures(app, "qwen3:4b")[1] is not None  # the model asked for still fails on its own


@pytest.mark.parametrize("status", [None, 500])
def test_genuine_model_failures_still_count(app, make_user, fake_ollama, status):
    setup_models(app)
    user = make_user("someone")
    if status:
        fake_ollama.fail_models = {"qwen3:4b"}
    else:
        fake_ollama.chat_records = [{"error": "model runner has unexpectedly stopped"}]
    _generate(app, "qwen3:4b", user)
    assert _failures(app, "qwen3:4b")[0] == 1


def test_only_errors_about_the_request_are_blamed_on_it():
    from bananachat.services import inference
    from bananachat.services.upstream import UpstreamError

    text = inference.TextRequest(user=None, model=None, messages=[{"role": "user", "content": "Hi"}])
    with_image = inference.TextRequest(user=None, model=None, messages=[{"role": "user", "content": "Hi",
                                                                         "images": ["AAAA"]}])
    assert inference._request_fault(UpstreamError("bad request", 400), text)
    assert inference._request_fault(UpstreamError("the prompt is too long for this model"), text)
    assert inference._request_fault(UpstreamError("failed to process inputs: image: unknown format"), with_image)
    assert not inference._request_fault(UpstreamError("failed to load image projector"), text)
    assert not inference._request_fault(UpstreamError("The backend took too long to respond.", kind="timeout"),
                                        with_image)
    assert not inference._request_fault(UpstreamError("model failed to load", 500), text)
