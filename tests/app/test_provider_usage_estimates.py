"""External API answers: usage estimated the same way whether they complete or not, and no database read per chunk."""

from __future__ import annotations

import pytest

from tests.app.test_provider_dialects import FakeProvider, make_model, make_provider, openai_text, run  # noqa: F401
from tests.app.test_provider_dialects import fake  # noqa: F401  (fixture)

# A reasoning model on a server that reports no usage: the role first, then nothing while it thinks.
SILENT_REASONING_WITHOUT_USAGE = [
    {'choices': [{'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]},
    {'choices': [{'delta': {'content': 'The answer.'}, 'finish_reason': None}]},
    {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
]


def test_a_completed_answer_without_usage_is_charged_for_the_reasoning_it_did_not_stream(app, fake, make_user):  # noqa: F811
    from bananachat.db import catalog
    from bananachat.db.credits import estimate_tokens
    from bananachat.services import inference
    from bananachat.services.upstream import CancelToken

    provider = make_provider(app, fake)
    model = make_model(app, provider, reasoning=['low', 'medium', 'high'])
    user = make_user('thinker')
    fake.events = SILENT_REASONING_WITHOUT_USAGE
    fake.delay = 2.0  # the provider thinks two seconds before its first visible output
    with app.app_context():
        request = inference.TextRequest(user=user, model=catalog.get(model['id']),
                                        messages=[{'role': 'user', 'content': 'x' * 400}],
                                        options={'num_predict': 4096})
        finished = list(inference.generate(request, CancelToken()))[-1]
    assert finished.state == 'completed' and finished.usage_estimated
    assert finished.prompt_tokens == 100
    visible = estimate_tokens('The answer.')
    # As for an interrupted answer: what was shown, plus the conservative rate for the time it reasoned.
    assert visible + 2 * inference.HIDDEN_REASONING_TOKENS_PER_SECOND <= finished.completion_tokens <= 4096


def test_a_completed_answer_without_usage_from_a_model_asked_not_to_reason_is_charged_for_its_text(
        app, fake, make_user):  # noqa: F811
    from bananachat.db import catalog
    from bananachat.db.credits import estimate_tokens
    from bananachat.services import inference
    from bananachat.services.upstream import CancelToken

    provider = make_provider(app, fake)
    model = make_model(app, provider)  # not a reasoning model
    user = make_user('plain')
    fake.events = SILENT_REASONING_WITHOUT_USAGE
    fake.delay = 0.5
    with app.app_context():
        request = inference.TextRequest(user=user, model=catalog.get(model['id']),
                                        messages=[{'role': 'user', 'content': 'Hi'}])
        finished = list(inference.generate(request, CancelToken()))[-1]
    assert finished.state == 'completed' and finished.completion_tokens == estimate_tokens('The answer.')


def test_a_streamed_answer_rereads_its_provider_only_now_and_then(app, fake, monkeypatch):  # noqa: F811
    from bananachat.db import external_providers as store

    provider = make_provider(app, fake)
    model = make_model(app, provider)
    fake.events = openai_text(*(['word '] * 400))
    reads = []
    original = store.get
    monkeypatch.setattr(store, 'get', lambda provider_id: reads.append(provider_id) or original(provider_id))
    chunks = run(app, model)
    assert ''.join(chunk.content for chunk in chunks).count('word') == 400 and chunks[-1].done
    # Once when the request starts and once more for its last chunk; never once per chunk.
    assert len(reads) <= 4, len(reads)


def test_a_provider_disabled_while_it_streams_still_ends_the_answer_soon(app, fake):  # noqa: F811
    import time

    from bananachat.db import catalog
    from bananachat.db import external_providers as store
    from bananachat.services import external_api
    from bananachat.services import external_providers as service
    from bananachat.services.upstream import UpstreamError

    provider = make_provider(app, fake)
    model = make_model(app, provider)
    fake.events = openai_text(*(['word '] * 200))
    fake.delay = 0.05  # ten seconds in all
    with app.app_context():
        stream = service.stream(catalog.get(model['id']), [{'role': 'user', 'content': 'Hi'}])
        assert next(stream).content == 'word '
        store.save(provider['id'], enabled=0)
        disabled = time.monotonic()
        with pytest.raises(UpstreamError) as caught:
            for _chunk in stream:
                pass
        stream.close()
    assert caught.value.code == 'provider_disabled'
    assert time.monotonic() - disabled < external_api.PROVIDER_RECHECK_SECONDS + 2
