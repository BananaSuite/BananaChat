"""The OpenAI-compatible API under /v1."""

from __future__ import annotations

import base64
import json

import pytest

def _png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buffer, format="PNG")
    return buffer.getvalue()


PNG = _png()
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


# ----- helpers --------------------------------------------------------------------

def roll_out(app, **fields):
    """Sync the fake Ollama models into the catalog and publish them."""
    from bananachat import db
    from bananachat.db import catalog
    from bananachat.services import ollama

    with app.app_context():
        ollama.sync_catalog()
        for model in catalog.list_models():
            catalog.set_rollout(model["id"], True)
            if fields:
                catalog.update(model["id"], **fields)
        db.execute("DELETE FROM inference_queue")


def token_for(app, user, name="test"):
    from bananachat.db import tokens

    with app.app_context():
        return tokens.create(user["id"], name)[1]


def call(app, method, path, token=None, payload=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        kwargs["json"] = payload
    return app.test_client().open(path, method=method, headers=headers, **kwargs)


def chat(app, token, **body):
    body.setdefault("model", "auto")
    body.setdefault("messages", [{"role": "user", "content": "Hi"}])
    return call(app, "POST", "/v1/chat/completions", token, body)


def events(response):
    """Decode an SSE body into JSON payloads (and the literal '[DONE]')."""
    items = []
    for block in response.get_data(as_text=True).split("\n\n"):
        if not block.startswith("data: "):
            continue
        data = block[6:]
        items.append(data if data == "[DONE]" else json.loads(data))
    return items


def ledger(app, user):
    from bananachat import db

    with app.app_context():
        return [row.to_dict() for row in db.query("SELECT * FROM credit_ledger WHERE user_id=? ORDER BY id",
                                                  (user["id"],))]


def assert_error(response, status, error_type, code=None):
    assert response.status_code == status, response.get_data(as_text=True)[:500]
    error = response.json["error"]
    assert error["type"] == error_type and error["message"]
    if code:
        assert error["code"] == code


@pytest.fixture
def user(app, make_user):
    roll_out(app)
    return make_user("dev")


@pytest.fixture
def token(app, user):
    return token_for(app, user)


# ----- authentication and limits -----------------------------------------------------

def test_missing_and_invalid_tokens_are_rejected(app, token):
    assert_error(call(app, "GET", "/v1/models"), 401, "authentication_error", "invalid_api_key")
    assert_error(call(app, "GET", "/v1/models", "bc-not-a-real-token"), 401, "authentication_error")
    response = call(app, "GET", "/v1/models", headers={"Authorization": f"Basic {token}"})
    assert_error(response, 401, "authentication_error")
    assert call(app, "GET", "/v1/models", token).status_code == 200


def test_invalid_tokens_are_throttled_per_address(app, token):
    for _ in range(30):
        assert call(app, "GET", "/v1/models", "bc-wrong").status_code == 401
    response = call(app, "GET", "/v1/models", "bc-wrong")
    assert_error(response, 429, "rate_limit_error")
    assert response.headers["Retry-After"] == "60"
    assert call(app, "GET", "/v1/models", token).status_code == 200  # valid tokens are unaffected


def test_suspended_accounts_get_permission_error(app, user, token):
    from bananachat.db import users

    with app.app_context():
        users.suspend(user["id"])
    assert_error(call(app, "GET", "/v1/models", token), 403, "permission_error", "account_suspended")


def test_revoked_tokens_stop_working(app, user, token):
    from bananachat.db import tokens

    with app.app_context():
        token_id = tokens.list_for(user["id"])[0]["id"]
        tokens.revoke(token_id, user["id"])
    assert_error(call(app, "GET", "/v1/models", token), 401, "authentication_error")


def _api_rate(requests, per, burst):
    from bananachat.db import limits

    policy = limits.get_policy("api")
    policy["rate"]["rules"] = [{"requests": requests, "per": per, "burst": burst}]
    limits.set_policy("api", policy, None)


def test_requests_are_rate_limited_per_account(app, user, token):
    with app.app_context():
        _api_rate(1, "minute", 2)  # two at once, then one a minute
    second_token = token_for(app, user, "second")
    assert call(app, "GET", "/v1/models", token).status_code == 200
    assert call(app, "GET", "/v1/models", second_token).status_code == 200
    response = call(app, "GET", "/v1/models", token)
    assert_error(response, 429, "rate_limit_error", "rate_limit_exceeded")
    assert response.headers["Retry-After"] == "60"
    assert response.headers["x-ratelimit-remaining-requests"] == "0"


def test_long_requests_leave_threads_for_health_checks(app, token):
    """Generating requests may never take the last few server threads."""
    from bananachat import security

    state = security._LongRequests()
    app.extensions["bananachat.long_requests"] = state
    limit = app.config["BC"].http_threads - security.RESERVED_THREADS
    state.active = limit
    response = chat(app, token)
    assert_error(response, 503, "server_error", "server_busy")
    assert response.headers["Retry-After"] == "5"
    assert app.test_client().get("/health").status_code == 200

    state.active = limit - 1
    streamed = chat(app, token, stream=True)
    assert streamed.status_code == 200 and state.active == limit
    assert events(streamed)[-1] == "[DONE]"
    streamed.close()
    assert state.active == limit - 1
    assert chat(app, token).status_code == 200 and state.active == limit - 1


def test_administrators_are_not_rate_limited(app):
    from bananachat.db import users

    roll_out(app)
    with app.app_context():
        _api_rate(1, "minute", 1)
        admin = users.get_by_username("admin")
    admin_token = token_for(app, admin)
    for _ in range(3):
        assert call(app, "GET", "/v1/models", admin_token).status_code == 200


def test_token_use_is_recorded(app, user, token):
    from bananachat.db import tokens

    call(app, "GET", "/v1/models", token)
    with app.app_context():
        assert tokens.list_for(user["id"])[0]["last_used_at"]


# ----- error envelope ------------------------------------------------------------------

def test_unknown_paths_and_methods_use_the_api_envelope(app, token):
    assert_error(call(app, "GET", "/v1/nothing-here", token), 404, "invalid_request_error", "unknown_endpoint")
    response = call(app, "POST", "/v1/models", token, {})
    assert_error(response, 405, "invalid_request_error", "method_not_allowed")
    assert response.headers["Allow"] == "GET"
    assert_error(call(app, "GET", "/v1/chat/completions", token), 405, "invalid_request_error")


def test_bodies_are_parsed_strictly(app, token):
    response = call(app, "POST", "/v1/chat/completions", token, data="hello", content_type="text/plain")
    assert_error(response, 415, "invalid_request_error", "unsupported_media_type")
    response = call(app, "POST", "/v1/chat/completions", token, data="{nope", content_type="application/json")
    assert_error(response, 400, "invalid_request_error", "invalid_json")
    assert_error(call(app, "POST", "/v1/chat/completions", token, [1, 2]), 400, "invalid_request_error")
    response = call(app, "POST", "/v1/chat/completions", token, data='{"temperature": NaN}',
                    content_type="application/json")
    assert_error(response, 400, "invalid_request_error", "invalid_json")


def test_oversized_bodies_are_refused_with_the_envelope(app, token):
    body = {"model": "auto", "messages": [{"role": "user", "content": "x" * (5 * 1024 * 1024)}]}
    assert_error(call(app, "POST", "/v1/chat/completions", token, body), 413, "invalid_request_error")


def test_maintenance_pauses_generation_but_not_the_api(app, user, token):
    from bananachat.db import settings, users

    with app.app_context():
        settings.update(maintenance_mode=1, maintenance_message="Back at noon.")
        admin = users.get_by_username("admin")
    assert call(app, "GET", "/v1/models", token).status_code == 200
    for path, body in (("/v1/chat/completions", {"messages": [{"role": "user", "content": "Hi"}]}),
                       ("/v1/images/generations", {"prompt": "A cat"})):
        response = call(app, "POST", path, token, body)
        assert_error(response, 503, "server_error", "maintenance")
        assert "Back at noon." in response.json["error"]["message"] and response.headers["Retry-After"]
    assert ledger(app, user) == []
    assert chat(app, token_for(app, admin)).status_code == 200  # administrators can still test


def test_outage_pauses_generation(make_app, fake_ollama, make_user):
    from bananachat.services import health

    app = make_app(OLLAMA_URL=fake_ollama.url.replace("127.0.0.1", "localhost.invalid"))
    user_token = None
    with app.app_context():
        health.record_probe(False, 1, "down")
        from bananachat import security
        from bananachat.db import users
        user_token = token_for(app, users.get(users.create("outaged", security.hash_password("x" * 12))))
    assert_error(chat(app, user_token), 503, "server_error", "outage")
    assert call(app, "GET", "/v1/models", user_token).status_code == 200
    with app.app_context():
        health.reset()


# ----- models ------------------------------------------------------------------------

def test_models_list_follows_access_policies(app, user, token):
    from bananachat.db import access, catalog

    listed = call(app, "GET", "/v1/models", token).json
    assert listed["object"] == "list"
    assert [item["id"] for item in listed["data"]] == ["auto", "llama3.2:3b", "qwen3:4b"]
    assert all(item["object"] == "model" and item["owned_by"] == "bananachat" for item in listed["data"])
    assert listed["data"][1]["created"] > 1_600_000_000

    with app.app_context():
        qwen = catalog.get_by_name("qwen3:4b")
        access.set_policy("model", qwen["id"], "deny_except_allowlist", True, None)
        llama = catalog.get_by_name("llama3.2:3b")
        catalog.set_rollout(llama["id"], False)
    assert [item["id"] for item in call(app, "GET", "/v1/models", token).json["data"]] == ["auto"]
    assert call(app, "GET", "/v1/models/qwen3:4b", token).status_code == 404
    with app.app_context():
        access.add_membership("model", qwen["id"], user["id"], "allowlist", added_by=None)
    assert call(app, "GET", "/v1/models/qwen3:4b", token).json["id"] == "qwen3:4b"


def test_category_scoped_to_chat_does_not_restrict_the_api(app, user, token):
    from bananachat.db import access, catalog

    with app.app_context():
        category = catalog.create_category("Internal", scope="chat")
        qwen = catalog.get_by_name("qwen3:4b")
        catalog.set_model_categories(qwen["id"], [category])
        access.set_policy("category", category, "deny_except_allowlist", True, None)
    ids = [item["id"] for item in call(app, "GET", "/v1/models", token).json["data"]]
    assert "qwen3:4b" in ids


# ----- chat completions ------------------------------------------------------------------

def test_completion_without_streaming(app, user, token, fake_ollama):
    response = chat(app, token)
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.json
    assert body["object"] == "chat.completion" and body["id"].startswith("chatcmpl-")
    assert body["model"] == "llama3.2:3b" and isinstance(body["created"], int)
    choice = body["choices"][0]
    assert choice == {"index": 0, "message": {"role": "assistant", "content": "Hello from the fake model."},
                      "logprobs": None, "finish_reason": "stop"}
    assert body["usage"] == {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
    rows = ledger(app, user)
    assert len(rows) == 1
    assert rows[0]["request_type"] == "api" and rows[0]["tokens_in"] == 11 and rows[0]["tokens_out"] == 5
    assert rows[0]["token_id"] is not None and rows[0]["credits_used"] == pytest.approx(0.016)
    from bananachat import db
    with app.app_context():
        metric = db.one("SELECT * FROM request_metrics WHERE user_id=?", (user["id"],))
        assert metric["request_type"] == "api" and metric["status"] == "ok" and metric["tokens_out"] == 5


def test_streamed_completion_chunks(app, user, token):
    response = chat(app, token, stream=True, stream_options={"include_usage": True})
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    items = events(response)
    assert items[-1] == "[DONE]"
    chunks = items[:-1]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert len({chunk["id"] for chunk in chunks}) == 1
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    content = "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks if chunk["choices"])
    assert content == "Hello from the fake model."
    final = [chunk for chunk in chunks if chunk["choices"] and chunk["choices"][0]["finish_reason"]]
    assert len(final) == 1 and final[0]["choices"][0]["finish_reason"] == "stop"
    usage = chunks[-1]
    assert usage["choices"] == [] and usage["usage"] == {"prompt_tokens": 11, "completion_tokens": 5,
                                                         "total_tokens": 16}
    assert len(ledger(app, user)) == 1


def test_stream_without_usage_option_sends_no_usage_chunk(app, token):
    items = events(chat(app, token, stream=True))
    assert items[-1] == "[DONE]"
    assert all(item["choices"] for item in items[:-1])


def test_options_and_stop_sequences_are_forwarded(app, token, fake_ollama):
    response = chat(app, token, temperature=0.3, top_p=0.5, seed=42, max_tokens=64, max_completion_tokens=32,
                    presence_penalty=0.5, frequency_penalty=-0.5, stop=["END", "###"])
    assert response.status_code == 200
    options = fake_ollama.chat_bodies()[-1]["options"]
    assert options["temperature"] == 0.3 and options["top_p"] == 0.5 and options["seed"] == 42
    assert options["num_predict"] == 32 and options["stop"] == ["END", "###"]
    assert options["presence_penalty"] == 0.5 and options["frequency_penalty"] == -0.5
    chat(app, token, stop="STOP")
    assert fake_ollama.chat_bodies()[-1]["options"]["stop"] == ["STOP"]


@pytest.mark.parametrize("body, param", [
    ({"temperature": 3}, "temperature"),
    ({"top_p": "high"}, "top_p"),
    ({"max_tokens": 0}, "max_tokens"),
    ({"stop": ["a", "b", "c", "d", "e"]}, "stop"),
    ({"n": 2}, "n"),
    ({"stream": "yes"}, "stream"),
    ({"tools": [{"type": "function", "function": {"name": "x"}}]}, "tools"),
    ({"messages": []}, "messages"),
    ({"messages": [{"role": "tool", "content": "x", "tool_call_id": "1"}]}, "messages[0]"),
    ({"messages": [{"role": "narrator", "content": "x"}]}, "messages[0].role"),
    ({"messages": [{"role": "user", "content": 5}]}, "messages[0].content"),
    ({"response_format": {"type": "json_schema"}}, "response_format"),
])
def test_invalid_parameters_are_rejected(app, token, body, param):
    response = chat(app, token, **body)
    assert_error(response, 400, "invalid_request_error")
    assert response.json["error"]["param"] == param


def test_message_and_context_limits(app, token):
    many = [{"role": "user", "content": "x"}] * 101
    assert_error(chat(app, token, messages=many), 400, "invalid_request_error", "too_many_messages")
    limit = app.config["BC"].chat_max_context_chars
    huge = [{"role": "user", "content": "x" * (limit + 1)}]
    assert_error(chat(app, token, messages=huge), 400, "invalid_request_error", "context_too_long")


def test_content_arrays_are_joined(app, token, fake_ollama):
    messages = [{"role": "user", "content": [{"type": "text", "text": "Line one"},
                                             {"type": "text", "text": "Line two"}]}]
    assert chat(app, token, messages=messages).status_code == 200
    sent = fake_ollama.chat_bodies()[-1]["messages"]
    assert sent[-1] == {"role": "user", "content": "Line one\nLine two"}


def test_images_need_a_vision_model_and_are_forwarded(app, user, token, fake_ollama):
    from bananachat.db import catalog

    messages = [{"role": "user", "content": [{"type": "text", "text": "What is this?"},
                                             {"type": "image_url", "image_url": {"url": PNG_URL}}]}]
    assert_error(chat(app, token, messages=messages), 503, "server_error")  # no vision model available
    with app.app_context():
        catalog.update(catalog.get_by_name("qwen3:4b")["id"], supports_vision=1)
    assert_error(chat(app, token, model="llama3.2:3b", messages=messages), 400, "invalid_request_error",
                 "model_not_suitable")
    response = chat(app, token, messages=messages)
    assert response.status_code == 200 and response.json["model"] == "qwen3:4b"
    sent = fake_ollama.chat_bodies()[-1]["messages"][-1]
    assert sent["content"] == "What is this?" and sent["images"] == [PNG_URL.split(",", 1)[1]]


@pytest.mark.parametrize("url, code", [
    ("https://example.com/cat.png", "unsupported_image"),
    ("data:image/gif;base64,R0lGODlh", "unsupported_image"),
    ("data:image/png;base64,not base64!", "invalid_image"),
    ("data:image/png;base64," + base64.b64encode(b"just text").decode(), "invalid_image"),
])
def test_only_inline_png_jpeg_webp_images_are_accepted(app, token, url, code):
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]
    assert_error(chat(app, token, messages=messages), 400, "invalid_request_error", code)


def test_model_system_prompt_is_prepended_unless_the_request_has_one(app, token, fake_ollama):
    from bananachat.db import catalog

    with app.app_context():
        catalog.update(catalog.get_by_name("llama3.2:3b")["id"], system_prompt="Be brief.")
    chat(app, token, model="llama3.2:3b")
    assert fake_ollama.chat_bodies()[-1]["messages"][0] == {"role": "system", "content": "Be brief."}
    chat(app, token, model="llama3.2:3b", messages=[{"role": "system", "content": "Mine."},
                                                    {"role": "user", "content": "Hi"}])
    sent = fake_ollama.chat_bodies()[-1]["messages"]
    assert [m["content"] for m in sent if m["role"] == "system"] == ["Mine."]


def test_reasoning_is_returned_separately(app, token, fake_ollama):
    fake_ollama.thinking = "Let me think."
    body = chat(app, token).json
    assert body["choices"][0]["message"]["reasoning_content"] == "Let me think. "
    assert body["choices"][0]["message"]["content"] == "Hello from the fake model."
    streamed = events(chat(app, token, stream=True))
    reasoning = "".join(item["choices"][0]["delta"].get("reasoning_content", "") for item in streamed[:-1])
    assert reasoning == "Let me think. "


def test_unknown_and_forbidden_models(app, user, token):
    from bananachat.db import access, catalog

    assert_error(chat(app, token, model="nope:1b"), 404, "invalid_request_error", "model_not_found")
    with app.app_context():
        model = catalog.get_by_name("qwen3:4b")
        access.set_policy("model", model["id"], "deny_except_allowlist", True, None)
    assert_error(chat(app, token, model="qwen3:4b"), 403, "permission_error", "model_not_allowed")


def test_exhausted_credits_are_refused(app, user, token):
    from bananachat.db import credits

    with app.app_context():
        credits.set_quota(user["id"], 0, 0, None)
    response = chat(app, token)
    assert_error(response, 429, "rate_limit_error", "insufficient_quota")
    assert int(response.headers["Retry-After"]) > 0
    assert ledger(app, user) == []


def test_backend_failures_are_reported_without_details(app, user, token, fake_ollama):
    fake_ollama.fail_models = {"llama3.2:3b", "qwen3:4b"}
    response = chat(app, token)
    assert_error(response, 502, "server_error", "upstream_error")
    assert "127.0.0.1" not in response.json["error"]["message"]
    streamed = chat(app, token, stream=True)
    assert_error(streamed, 502, "server_error")
    assert ledger(app, user) == []


def test_queue_full_is_reported(app, user, token):
    from bananachat import db

    with app.app_context():
        for index in range(3):
            db.execute("INSERT INTO inference_queue (req_id, priority, status, owner_pid, enqueued_at, "
                       "heartbeat_at, owner_key) VALUES (?, 1, 'waiting', 1, strftime('%s','now'), "
                       "strftime('%s','now') + 1000, ?)", (f"r{index}", f"user:{user['id']}:api"))
    response = chat(app, token)
    assert_error(response, 429, "rate_limit_error", "too_many_requests")
    assert response.headers["Retry-After"]


def test_interrupted_stream_is_charged_once(app, user, token, fake_ollama):
    fake_ollama.reply = " ".join(["word"] * 200)
    fake_ollama.chunk_delay = 0.01
    response = app.test_client().post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                                      json={"model": "auto", "stream": True,
                                            "messages": [{"role": "user", "content": "Count"}]}, buffered=False)
    stream = response.response
    received = [next(iter(stream)) for _ in range(5)]
    assert any(b"word" in part if isinstance(part, bytes) else "word" in part for part in received)
    response.close()
    response.close()  # repeated cleanup must not charge the same partial answer again
    rows = ledger(app, user)
    assert len(rows) == 1 and rows[0]["usage_estimated"] == 1 and rows[0]["tokens_out"] > 0
    from bananachat import db
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0
        assert db.scalar("SELECT status FROM request_metrics WHERE user_id=?", (user["id"],)) == "stopped"


def test_images_of_one_request_share_a_size_budget(app, monkeypatch):
    """Requests must stay under the compute proxy's body limit once the images are base64-encoded."""
    from bananachat.services import completions
    from bananachat.services.completions import CompletionError, parse_chat_request

    monkeypatch.setattr(completions, "MAX_CONTEXT_IMAGE_BYTES", len(PNG) + 10)
    one = {"type": "image_url", "image_url": {"url": PNG_URL}}
    body = {"model": "auto", "messages": [{"role": "user", "content": [{"type": "text", "text": "Look"}, one]}]}
    with app.app_context():
        assert parse_chat_request(body).image_count == 1
        body["messages"].append({"role": "user", "content": [one]})
        with pytest.raises(CompletionError) as caught:
            parse_chat_request(body)
    assert caught.value.status == 400 and caught.value.code == "images_too_large"


def test_the_image_budget_fits_the_compute_proxy_limit():
    import compute.inference_proxy as proxy
    from bananachat.services.upstream import MAX_CONTEXT_IMAGE_BYTES

    encoded = MAX_CONTEXT_IMAGE_BYTES * 4 // 3
    assert encoded + 4 * 1024 * 1024 < proxy.MAX_BODY  # room for the text of the conversation


def test_a_fallback_model_gets_its_own_system_prompt_and_options(app, token, fake_ollama):
    from bananachat.db import catalog

    with app.app_context():
        for model in catalog.list_models():
            catalog.update(model["id"], system_prompt=f"You are {model['ollama_name']}.",
                           temperature=0.3 if model["ollama_name"] == "llama3.2:3b" else 0.9)
    assert chat(app, token).status_code == 200
    first = fake_ollama.chat_bodies()[-1]["model"]
    fake_ollama.fail_models = {first}
    response = chat(app, token, stop=["END"])
    assert response.status_code == 200, response.get_data(as_text=True)
    fallback = fake_ollama.chat_bodies()[-1]
    assert fallback["model"] != first
    assert fallback["messages"][0] == {"role": "system", "content": f"You are {fallback['model']}."}
    assert fallback["options"]["temperature"] == (0.3 if fallback["model"] == "llama3.2:3b" else 0.9)
    assert fallback["options"]["stop"] == ["END"]
