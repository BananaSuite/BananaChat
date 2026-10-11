"""Interrupted answers are charged for what the provider worked on: hidden reasoning, images, any script."""

from __future__ import annotations

import base64
import threading
import time

from tests.app.test_provider_dialects import FakeProvider, make_model, make_provider  # noqa: F401
from tests.app.test_provider_dialects import fake  # noqa: F401  (fixture)

SILENT_REASONING = [
    # A reasoning model's stream: the role first, then nothing while it thinks (the fake waits between events).
    {'choices': [{'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]},
    {'choices': [{'delta': {'content': 'The answer.'}, 'finish_reason': None}]},
    {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
    {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 900}},
]


def _reasoning_model(app, fake):
    provider = make_provider(app, fake)
    return make_model(app, provider, reasoning=['low', 'medium', 'high'])


def _ledger(app, user):
    from bananachat import db

    with app.app_context():
        return [row.to_dict() for row in db.query("SELECT * FROM credit_ledger WHERE user_id=?", (user["id"],))]


def test_estimates_count_cjk_text_and_images():
    from bananachat.db.credits import IMAGE_TOKEN_ESTIMATE, estimate_tokens
    from bananachat.services import inference

    assert estimate_tokens("a" * 400) == 100
    assert estimate_tokens("日本語の文章です。" * 10) == 90  # about one token per character
    assert estimate_tokens("Hello 世界") == 2 + 2
    image = base64.b64encode(b"\x89PNG....").decode()
    request = inference.TextRequest(user=None, model=None, messages=[
        {"role": "user", "content": "a" * 40, "images": [image, image]}])
    prompt, completion = inference.interrupted_usage(request, "")
    assert prompt == 10 + 2 * IMAGE_TOKEN_ESTIMATE and completion == 0


def test_stopping_an_external_model_while_it_reasons_silently_is_charged(app, fake, make_user):  # noqa: F811
    from bananachat.db import catalog
    from bananachat.services import inference
    from bananachat.services.upstream import CancelToken

    model = _reasoning_model(app, fake)
    user = make_user("thinker")
    fake.events = SILENT_REASONING
    fake.delay = 3.0
    cancel = CancelToken()
    with app.app_context():
        request = inference.TextRequest(user=user, model=catalog.get(model["id"]),
                                        messages=[{"role": "user", "content": "x" * 400}],
                                        options={"num_predict": 4096})
        events = []
        for event in inference.generate(request, cancel):
            events.append(event)
            if isinstance(event, inference.Started):  # stopped 1.5 s after the request reached the provider
                threading.Timer(1.5, cancel.cancel, args=("stopped",)).start()
        finished = events[-1]
    assert finished.state == "stopped" and finished.billable and finished.usage_estimated
    assert finished.prompt_tokens == 100
    # At least the conservative rate for the time it reasoned, at most its output limit.
    assert 1.4 * inference.HIDDEN_REASONING_TOKENS_PER_SECOND <= finished.completion_tokens <= 4096


def test_hidden_reasoning_is_added_to_the_visible_answer_and_capped(app, fake, make_user):  # noqa: F811
    from bananachat.db import catalog
    from bananachat.services import inference
    from bananachat.services.upstream import CancelToken

    model = _reasoning_model(app, fake)
    user = make_user("thinker")
    fake.events = SILENT_REASONING
    fake.delay = 2.0
    cancel = CancelToken()
    with app.app_context():
        request = inference.TextRequest(user=user, model=catalog.get(model["id"]),
                                        messages=[{"role": "user", "content": "Hi"}], options={"num_predict": 30})
        events = []
        for event in inference.generate(request, cancel):
            events.append(event)
            if isinstance(event, inference.Delta):
                cancel.cancel("stopped")  # stopped right after the first visible text
    finished = events[-1]
    assert finished.state == "stopped" and finished.completion_tokens == 30  # capped by the output limit


def test_a_local_model_stopped_before_any_output_is_not_charged(app, make_user, fake_ollama):
    from bananachat.db import catalog
    from bananachat.services import inference
    from bananachat.services.upstream import CancelToken
    from tests.app.test_chat import setup_models

    setup_models(app, reasoning=True)
    user = make_user("local")
    fake_ollama.chunk_delay = 0.5
    cancel = CancelToken()
    with app.app_context():
        request = inference.TextRequest(user=user, model=catalog.get_by_name("qwen3:4b"),
                                        messages=[{"role": "user", "content": "Hi"}])
        events = []
        for event in inference.generate(request, cancel):
            events.append(event)
            if isinstance(event, inference.Started):  # stopped once it runs (not while it waits for its turn)
                threading.Timer(0.2, cancel.cancel, args=("stopped",)).start()
        finished = events[-1]
    assert finished.state == "stopped" and not finished.billable


def test_the_chat_charges_an_answer_stopped_while_the_model_reasons(app, fake, make_user):  # noqa: F811
    from tests.app.test_chat import new_chat, read_events, send, user_browser, wait_idle

    model = _reasoning_model(app, fake)
    fake.events = SILENT_REASONING
    fake.delay = 3.0
    browser = user_browser(app, make_user, "thinker")
    session_id = new_chat(browser)
    response = send(browser, session_id, "Think hard", buffered=False, model=model["ollama_name"])
    read_events(response, until=lambda event: event["type"] == "start")
    time.sleep(1.2)
    assert browser.post(f"/chat/{session_id}/stop", {}, headers={"X-Requested-With": "fetch"}).status_code == 200
    events = read_events(response)
    assert events[-1]["state"] == "stopped" and events[-1]["tokens_out"] >= 20
    wait_idle(app, session_id)
    with app.app_context():
        from bananachat.db import users

        rows = _ledger(app, users.get_by_username("thinker"))
    assert len(rows) == 1 and rows[0]["tokens_out"] >= 20 and rows[0]["tokens_in"] > 0


def test_an_api_client_leaving_after_hidden_reasoning_is_charged_for_it(app, fake, make_user):  # noqa: F811
    """The API notices a client that left when it writes, so after the first output that follows the reasoning."""
    from bananachat.services import completions, inference

    model = _reasoning_model(app, fake)
    fake.events = SILENT_REASONING
    fake.delay = 2.0
    user = make_user("leaver")
    with app.test_request_context():
        params = completions.parse_chat_request({"model": model["ollama_name"], "stream": True,
                                                 "messages": [{"role": "user", "content": "x" * 400}]})
        run = completions.CompletionRun(user, params, request_type="api")
        run.prime(until=(inference.Delta, inference.Finished))  # what the API does before it answers
        run.close()  # writing the first output failed: the client disconnected
    rows = _ledger(app, user)
    assert len(rows) == 1 and rows[0]["tokens_in"] == 100
    assert rows[0]["tokens_out"] >= estimate("The answer.") + 1.9 * inference.HIDDEN_REASONING_TOKENS_PER_SECOND


def estimate(text):
    from bananachat.db.credits import estimate_tokens

    return estimate_tokens(text)
