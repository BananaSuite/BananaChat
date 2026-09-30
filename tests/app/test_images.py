"""Image generation through ComfyUI: the API endpoint, the Images page and the client."""

from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.app.conftest import Browser
from tests.app.test_api import assert_error, call, events, ledger, token_for

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x01" * 40
CHECKPOINT = "sdxl_base.safetensors"
MODEL_ID = f"comfyui:{CHECKPOINT}"


class FakeComfyUI:
    """A small imitation of the ComfyUI HTTP API.

    ``mode``: ``ok`` (image after ``polls`` history polls), ``fail`` (execution
    error), ``hang`` (never finishes), ``not_image`` (``/view`` returns text) or
    ``reject`` (``/prompt`` refuses the workflow). ``running`` controls whether
    the prompt shows as running or pending in ``/queue``.
    """

    def __init__(self):
        self.checkpoints = [CHECKPOINT]
        self.mode = "ok"
        self.polls = 2
        self.running = True
        self.image = PNG
        self.on_view = None
        self.prompts: list[dict] = []
        self.calls: list[tuple[str, str, object]] = []
        self._polled: dict[str, int] = {}
        self._server = None

    @property
    def url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def posted(self, path):
        return [body for method, called, body in self.calls if method == "POST" and called == path]

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, status, payload, content_type="application/json"):
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                parts = urlsplit(self.path)
                fake.calls.append(("GET", parts.path, parts.query))
                if parts.path == "/object_info/CheckpointLoaderSimple":
                    self._send(200, {"CheckpointLoaderSimple": {"input": {"required": {
                        "ckpt_name": [fake.checkpoints, {}]}}}})
                elif parts.path.startswith("/history/"):
                    self._history(parts.path.rsplit("/", 1)[1])
                elif parts.path == "/queue":
                    ids = [prompt["id"] for prompt in fake.prompts]
                    entries = [[index, prompt_id, {}, {}, ["7"]] for index, prompt_id in enumerate(ids)]
                    self._send(200, {"queue_running": entries[-1:] if fake.running and entries else [],
                                     "queue_pending": [] if fake.running else entries[-1:]})
                elif parts.path == "/view":
                    query = parse_qs(parts.query)
                    assert query["type"] == ["temp"] and query["filename"] == ["bc_00001_.png"]
                    if fake.on_view:
                        fake.on_view()
                    if fake.mode == "not_image":
                        self._send(200, b"<html>nope</html>", "text/html")
                    else:
                        self._send(200, fake.image, "image/png")
                elif parts.path == "/system_stats":
                    self._send(200, {"system": {"comfyui_version": "0.3.40"},
                                     "devices": [{"name": "cuda:0 RTX", "vram_total": 10, "vram_free": 5}]})
                else:
                    self._send(404, {"error": "not found"})

            def _history(self, prompt_id):
                count = fake._polled.get(prompt_id, 0) + 1
                fake._polled[prompt_id] = count
                if fake.mode == "hang" or count <= fake.polls:
                    self._send(200, {})
                elif fake.mode == "fail":
                    self._send(200, {prompt_id: {"outputs": {}, "status": {
                        "status_str": "error", "completed": False,
                        "messages": [["execution_error", {"exception_message": "CUDA out of memory at 10.0.0.7"}]]}}})
                else:
                    self._send(200, {prompt_id: {"outputs": {"7": {"images": [
                        {"filename": "bc_00001_.png", "subfolder": "", "type": "temp"}]}},
                        "status": {"status_str": "success", "completed": True}}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                fake.calls.append(("POST", self.path, body))
                if self.path == "/prompt":
                    if fake.mode == "reject":
                        self._send(400, {"error": {"message": "Prompt outputs failed validation"},
                                         "node_errors": {"5": {}}})
                        return
                    prompt_id = f"p{len(fake.prompts) + 1}"
                    fake.prompts.append({"id": prompt_id, "workflow": body["prompt"]})
                    self._send(200, {"prompt_id": prompt_id, "number": len(fake.prompts), "node_errors": {}})
                elif self.path in ("/interrupt", "/queue", "/history"):
                    self._send(200, b"", "text/plain")
                else:
                    self._send(404, {"error": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def comfy():
    server = FakeComfyUI().start()
    yield server
    server.stop()


@pytest.fixture
def image_app(make_app, comfy):
    from bananachat.db import catalog
    from bananachat.services import comfyui

    app = make_app(IMAGE_BACKEND="comfyui", COMFYUI_URL=comfy.url, COMFYUI_POLL_INTERVAL="0.05",
                   COMFYUI_STEPS="12", COMFYUI_SAMPLER="dpmpp_2m")
    with app.app_context():
        assert comfyui.sync_models() == 1
        catalog.set_rollout(catalog.get_by_name(MODEL_ID)["id"], True)
    return app


def create_user(app, username):
    """Create an account in *app* (``make_user`` would build a second app)."""
    from bananachat import security
    from bananachat.db import users

    with app.test_request_context():
        return users.get(users.create(username, security.hash_password(f"{username}-password")))


@pytest.fixture
def artist(image_app):
    return create_user(image_app, "artist")


@pytest.fixture
def artist_token(image_app, artist):
    return token_for(image_app, artist)


def generate(app, token, **body):
    body.setdefault("prompt", "A lighthouse at dusk")
    return call(app, "POST", "/v1/images/generations", token, body)


def reservations(app):
    from bananachat import db

    with app.app_context():
        return db.scalar("SELECT COUNT(*) FROM image_credit_reservations", default=0)


# ----- API ----------------------------------------------------------------------------

def test_image_generation_end_to_end(image_app, artist, artist_token, comfy):
    response = generate(image_app, artist_token, size="768x512", model=MODEL_ID)
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.json
    assert base64.b64decode(body["data"][0]["b64_json"]) == PNG
    assert body["model"] == MODEL_ID and body["output_format"] == "png" and body["size"] == "768x512"
    assert body["usage"]["output_tokens"] == 5000 and "credits" not in body["usage"]

    workflow = comfy.prompts[0]["workflow"]
    assert [workflow[str(n)]["class_type"] for n in range(1, 8)] == [
        "CheckpointLoaderSimple", "CLIPTextEncode", "CLIPTextEncode", "EmptyLatentImage", "KSampler", "VAEDecode",
        "PreviewImage"]
    assert workflow["1"]["inputs"]["ckpt_name"] == CHECKPOINT
    assert workflow["2"]["inputs"]["text"] == "A lighthouse at dusk"
    assert (workflow["4"]["inputs"]["width"], workflow["4"]["inputs"]["height"]) == (768, 512)
    assert workflow["5"]["inputs"]["steps"] == 12 and workflow["5"]["inputs"]["sampler_name"] == "dpmpp_2m"
    assert comfy.posted("/history") == [{"delete": ["p1"]}]
    assert comfy.posted("/interrupt") == []

    rows = ledger(image_app, artist)
    assert len(rows) == 1 and rows[0]["credits_used"] == 5 and rows[0]["request_type"] == "api"
    assert rows[0]["token_id"] is not None and rows[0]["model_id"] is not None
    assert reservations(image_app) == 0
    from bananachat import db
    with image_app.app_context():
        metric = db.one("SELECT * FROM request_metrics WHERE request_type='image'")
        assert metric["status"] == "ok" and metric["user_id"] == artist["id"]
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0


def test_seeds_are_random(image_app, artist_token, comfy):
    generate(image_app, artist_token)
    generate(image_app, artist_token)
    seeds = {prompt["workflow"]["5"]["inputs"]["seed"] for prompt in comfy.prompts}
    assert len(seeds) == 2


def test_failures_refund_the_reservation(image_app, artist, artist_token, comfy):
    comfy.mode = "fail"
    response = generate(image_app, artist_token)
    assert_error(response, 502, "server_error", "upstream_error")
    assert "CUDA out of memory" in response.json["error"]["message"]
    assert "10.0.0.7" not in response.json["error"]["message"]
    assert ledger(image_app, artist) == [] and reservations(image_app) == 0
    assert comfy.posted("/history") == [{"delete": ["p1"]}]

    comfy.mode = "reject"
    assert_error(generate(image_app, artist_token), 502, "server_error")
    comfy.mode = "not_image"
    assert_error(generate(image_app, artist_token), 502, "server_error")
    assert ledger(image_app, artist) == [] and reservations(image_app) == 0


def test_unexpected_errors_refund_the_reservation(image_app, artist, artist_token, monkeypatch):
    from bananachat.services import comfyui

    def explode(*args, **kwargs):
        raise KeyError("surprise")
        yield  # pragma: no cover

    monkeypatch.setattr(comfyui, "generate", explode)
    response = generate(image_app, artist_token)
    assert_error(response, 500, "server_error")
    assert reservations(image_app) == 0 and ledger(image_app, artist) == []


def test_timeouts_interrupt_the_running_prompt(make_app, comfy):
    from bananachat.db import catalog
    from bananachat.services import comfyui

    app = make_app(IMAGE_BACKEND="comfyui", COMFYUI_URL=comfy.url, COMFYUI_POLL_INTERVAL="0.05",
                   COMFYUI_GENERATION_TIMEOUT="1")
    with app.app_context():
        comfyui.sync_models()
        catalog.set_rollout(catalog.get_by_name(MODEL_ID)["id"], True)
    user = create_user(app, "slowpoke")
    token = token_for(app, user)
    comfy.mode = "hang"
    assert_error(generate(app, token), 504, "server_error", "timeout")
    assert comfy.posted("/interrupt") == [{"prompt_id": "p1"}]
    assert comfy.posted("/history") == [{"delete": ["p1"]}]
    assert reservations(app) == 0 and ledger(app, user) == []

    comfy.running = False  # queued behind someone else's prompt: remove it, never interrupt
    assert_error(generate(app, token), 504, "server_error")
    assert comfy.posted("/queue") == [{"delete": ["p2"]}]
    assert len(comfy.posted("/interrupt")) == 1


def test_expired_reservations_are_still_charged(image_app, artist, artist_token, comfy):
    from bananachat import db

    def expire():
        db.execute("DELETE FROM image_credit_reservations")

    comfy.on_view = expire
    assert generate(image_app, artist_token).status_code == 200
    comfy.on_view = None
    rows = ledger(image_app, artist)
    assert len(rows) == 1 and rows[0]["credits_used"] == 5


def test_insufficient_credits_are_refused_before_generation(image_app, artist, artist_token, comfy):
    from bananachat.db import credits

    with image_app.app_context():
        credits.set_quota(artist["id"], 4000, 0, None)
    response = generate(image_app, artist_token)
    assert_error(response, 429, "rate_limit_error", "insufficient_quota")
    assert comfy.prompts == [] and reservations(image_app) == 0


def test_image_rate_limit(make_app, comfy):
    from bananachat.db import catalog
    from bananachat.services import comfyui

    app = make_app(IMAGE_BACKEND="comfyui", COMFYUI_URL=comfy.url, COMFYUI_POLL_INTERVAL="0.05",
                   IMAGE_GENERATION_RPM="1")
    with app.app_context():
        comfyui.sync_models()
        catalog.set_rollout(catalog.get_by_name(MODEL_ID)["id"], True)
    token = token_for(app, create_user(app, "quick"))
    assert generate(app, token).status_code == 200
    response = generate(app, token)
    assert_error(response, 429, "rate_limit_error", "rate_limit_exceeded")
    assert response.headers["Retry-After"] == "60"


@pytest.mark.parametrize("body, param", [
    ({"prompt": ""}, "prompt"),
    ({"prompt": "x" * 10_001}, "prompt"),
    ({"size": "1001x1000"}, "size"),
    ({"size": "4096x4096"}, "size"),
    ({"size": "128x128"}, "size"),
    ({"size": "big"}, "size"),
    ({"n": 2}, "n"),
    ({"response_format": "url"}, "response_format"),
])
def test_invalid_image_requests(image_app, artist_token, comfy, body, param):
    response = generate(image_app, artist_token, **body)
    assert_error(response, 400, "invalid_request_error")
    assert response.json["error"]["param"] == param
    assert comfy.prompts == []


def test_image_models_follow_access_policies(image_app, artist, artist_token):
    from bananachat.db import access

    listed = [item["id"] for item in call(image_app, "GET", "/v1/models", artist_token).json["data"]]
    assert MODEL_ID in listed
    with image_app.app_context():
        access.set_policy("image_generation", 0, "deny_except_allowlist", True, None)
    assert MODEL_ID not in [item["id"] for item in call(image_app, "GET", "/v1/models", artist_token).json["data"]]
    assert_error(generate(image_app, artist_token, model=MODEL_ID), 403, "permission_error")
    assert_error(generate(image_app, artist_token), 503, "server_error", "model_unavailable")
    assert_error(generate(image_app, artist_token, model="llama3.2:3b"), 404, "invalid_request_error")


def test_text_models_cannot_generate_images(image_app, artist_token):
    from bananachat.db import catalog
    from bananachat.services import ollama

    with image_app.app_context():
        ollama.sync_catalog()
        catalog.set_rollout(catalog.get_by_name("llama3.2:3b")["id"], True)
    assert_error(generate(image_app, artist_token, model="llama3.2:3b"), 400, "invalid_request_error",
                 "model_not_suitable")


def test_images_disabled(app, make_user):
    token = token_for(app, make_user("nopics"))
    assert_error(generate(app, token), 404, "invalid_request_error", "images_disabled")
    browser = Browser(app)
    browser.login("nopics")
    assert browser.get("/images").status_code == 404
    assert "/images" not in browser.get("/developer").get_data(as_text=True).split("<main")[0]


# ----- ComfyUI client -----------------------------------------------------------------

def test_sync_status_and_background_job(image_app, comfy):
    from bananachat.db import catalog
    from bananachat.services import background, comfyui

    assert "comfyui-sync" in background.jobs()
    with image_app.app_context():
        status = comfyui.status()
        assert status["reachable"] and status["version"] == "0.3.40" and status["checkpoints"] == 1
        assert status["last_sync"]["ok"] and status["devices"][0]["name"] == "cuda:0 RTX"
        comfy.checkpoints = []
        comfyui.sync_models()
        assert catalog.get_by_name(MODEL_ID)["backend_available"] == 0
        comfy.checkpoints = [CHECKPOINT, "other.safetensors"]
        background.jobs()["comfyui-sync"].function(image_app)
        assert catalog.get_by_name(MODEL_ID)["backend_available"] == 1
        assert catalog.get_by_name("comfyui:other.safetensors")["is_rolled_out"] == 0


def test_unreachable_comfyui_keeps_the_catalog(make_app):
    from bananachat.services import comfyui

    app = make_app(IMAGE_BACKEND="comfyui", COMFYUI_URL="http://127.0.0.1:9", COMFYUI_TIMEOUT="1")
    with app.app_context():
        with pytest.raises(comfyui.ComfyUIError):
            comfyui.sync_models()
        status = comfyui.status()
        assert not status["reachable"] and status["error"] and status["last_sync"]["ok"] is False


def test_unsafe_output_references_are_refused():
    from bananachat.services import comfyui

    for bad in ({"filename": "../x.png", "type": "temp"}, {"filename": "a.png", "subfolder": "../etc", "type": "temp"},
                {"filename": "a.png", "type": "input"}, {"filename": "a/b.png", "type": "temp"}):
        with pytest.raises(comfyui.ComfyUIError):
            comfyui._descriptor(bad)
    assert comfyui._descriptor({"filename": "a.png", "subfolder": "x/y", "type": "temp"})["subfolder"] == "x/y"


# ----- Images page ----------------------------------------------------------------------

def test_images_page_and_session_generation(image_app, artist, comfy):
    browser = Browser(image_app)
    browser.login("artist")
    browser._ensure_csrf()  # a real browser reads the token from the page it loads
    page = browser.get("/images")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert MODEL_ID in html and 'id="page-data"' in html and 'style="' not in html
    assert "images.js" in html

    response = browser.post_json("/images/generate", {"prompt": "A cat", "model": MODEL_ID, "size": "512x512"})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert base64.b64decode(response.json["b64_json"]) == PNG and response.json["mime_type"] == "image/png"

    streamed = browser.post_json("/images/generate", {"prompt": "A dog", "size": "512x512"},
                                 headers={"Accept": "text/event-stream"})
    items = events(streamed)
    kinds = [item["type"] for item in items]
    assert kinds[-1] == "done" and "started" in kinds and "progress" in kinds
    assert base64.b64decode(items[-1]["image"]["b64_json"]) == PNG
    rows = ledger(image_app, artist)
    assert len(rows) == 2 and all(row["token_id"] is None and row["credits_used"] == 5 for row in rows)


def test_session_generation_reports_errors(image_app, artist, comfy):
    browser = Browser(image_app)
    browser.login("artist")
    response = browser.post_json("/images/generate", {"prompt": "", "size": "512x512"})
    assert response.status_code == 400 and response.json["error"]["message"]
    comfy.mode = "fail"
    streamed = browser.post_json("/images/generate", {"prompt": "A dog"}, headers={"Accept": "text/event-stream"})
    items = events(streamed)
    assert items[-1]["type"] == "error" and items[-1]["message"]
    assert reservations(image_app) == 0 and ledger(image_app, artist) == []


def test_abandoned_session_generation_is_refunded(image_app, artist, comfy):
    comfy.mode = "hang"
    browser = Browser(image_app)
    browser.login("artist")
    browser._ensure_csrf()
    response = browser.client.post("/images/generate", json={"prompt": "A dog"}, buffered=False,
                                   headers={"X-CSRF-Token": "test-csrf-token", "X-Requested-With": "fetch",
                                            "Accept": "text/event-stream"})
    iterator = iter(response.response)
    for _ in range(3):
        next(iterator)
    assert reservations(image_app) == 1
    response.close()
    time.sleep(0.1)
    assert reservations(image_app) == 0
    assert comfy.posted("/interrupt") == [{"prompt_id": "p1"}]


def test_images_are_paused_during_maintenance(image_app, artist, artist_token, comfy):
    from bananachat.db import settings

    with image_app.app_context():
        settings.update(maintenance_mode=1)
    assert_error(generate(image_app, artist_token), 503, "server_error", "maintenance")
    browser = Browser(image_app)
    browser.login("artist")
    browser._ensure_csrf()
    assert 'id="img-generate" disabled' in browser.get("/images").get_data(as_text=True)
    response = browser.post_json("/images/generate", {"prompt": "A cat"})
    assert response.status_code == 503 and response.json["error"]["code"] == "maintenance"
    assert comfy.prompts == [] and reservations(image_app) == 0
