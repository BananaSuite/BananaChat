"""Claude's signed thinking blocks are passed back unmodified with the tool_use turn they belong to.

Anthropic's Messages API needs the thinking blocks of the assistant turn that
asked for tools when the next request (with the tool results) asks for
thinking again. Newer models also bind each block to everything sent before it
(the top-level system prompt, the tools and earlier messages), so a block is
replayed only while that prefix is unchanged; otherwise the continuation runs
without a thinking request, as before. Every server here is a local fake.
"""
from __future__ import annotations

import copy

import pytest

from bananachat.services import external_api as api
from tests.app.test_provider_dialects import anthropic_text, done, fake, make_model, make_provider, run  # noqa: F401

TOOL = {'type': 'function', 'function': {'name': 'read_file', 'description': 'Read a file',
                                         'parameters': {'type': 'object'}}}
SYSTEM = {'role': 'system', 'content': 'You are a careful agent.'}
USER = {'role': 'user', 'content': 'Read a file'}


def thinking_tool_turn(*, signature='sig-1', thinking='Plan', text='Reading it.', tool_id='toolu_01',
                       path='a', thinking_blocks=True):
    events = [{'type': 'message_start', 'message': {'usage': {'input_tokens': 10, 'output_tokens': 1}}}]
    index = 0
    if thinking_blocks:
        events += [
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': thinking}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta',
                                                                  'signature': signature}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'redacted_thinking',
                                                                          'data': 'opaque-' + signature}},
            {'type': 'content_block_stop', 'index': 1}]
        index = 2
    events += [
        {'type': 'content_block_start', 'index': index, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'text_delta', 'text': text}},
        {'type': 'content_block_stop', 'index': index},
        {'type': 'content_block_start', 'index': index + 1,
         'content_block': {'type': 'tool_use', 'id': tool_id, 'name': 'read_file', 'input': {}}},
        {'type': 'content_block_delta', 'index': index + 1,
         'delta': {'type': 'input_json_delta', 'partial_json': '{"path": "%s"}' % path}},
        {'type': 'content_block_stop', 'index': index + 1},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 20}},
        {'type': 'message_stop'}]
    return events


def answer(app, fake, model, history, *, events=None, tools=(TOOL,), effort='high'):
    """Run one request; return its final chunk."""
    fake.events = events if events is not None else thinking_tool_turn()
    return done(run(app, model, history, tools=list(tools), effort=effort))


def with_results(history, final, *, text='Reading it.', path='a', result='file data', name='read_file'):
    """The agent loop's history after it ran the tool *final* asked for."""
    assistant = {'role': 'assistant', 'content': text,
                 'tool_calls': [{'function': {'name': name, 'arguments': {'path': path}}}]}
    if final.extra.get('replay') is not None:
        assistant['replay'] = final.extra['replay']
    return [*history, assistant, {'role': 'tool', 'tool_name': name, 'content': result}]


def last_body(fake):
    return fake.posts()[-1]['body']


SIGNED = [{'type': 'thinking', 'thinking': 'Plan', 'signature': 'sig-1'},
          {'type': 'redacted_thinking', 'data': 'opaque-sig-1'}]
VISIBLE = [{'type': 'text', 'text': 'Reading it.'},
           {'type': 'tool_use', 'id': 'toolu_01', 'name': 'read_file', 'input': {'path': 'a'}}]


@pytest.fixture
def claude(app, fake):
    provider = make_provider(app, fake, protocol='anthropic')
    return make_model(app, provider, model='claude-adaptive', tools=True, reasoning=['off', 'low', 'high'])


def test_tool_continuation_replays_signed_thinking_and_asks_for_thinking_again(app, fake, claude):
    first = answer(app, fake, claude, [SYSTEM, USER])
    history = with_results([SYSTEM, USER], first)
    answer(app, fake, claude, history, events=anthropic_text('Done'))
    body = last_body(fake)
    assert body['messages'][1] == {'role': 'assistant', 'content': SIGNED + VISIBLE}
    assert body['messages'][2]['content'][0]['tool_use_id'] == 'toolu_01'
    assert body['thinking'] == {'type': 'adaptive', 'display': 'summarized'}
    assert body['output_config'] == {'effort': 'high'}


def test_budget_thinking_models_also_keep_thinking_in_tool_rounds(app, fake):
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider, model='claude-budget', tools=True, reasoning=['off', 'high'],
                       thinking='budget')
    first = answer(app, fake, model, [SYSTEM, USER])
    answer(app, fake, model, with_results([SYSTEM, USER], first), events=anthropic_text('Done'))
    body = last_body(fake)
    assert body['messages'][1]['content'][:2] == SIGNED
    assert body['thinking']['type'] == 'enabled' and body['thinking']['budget_tokens'] >= 1024


def test_every_turn_of_a_long_tool_round_is_replayed(app, fake, claude):
    first = answer(app, fake, claude, [SYSTEM, USER])
    history = with_results([SYSTEM, USER], first)
    second = answer(app, fake, claude, history, events=thinking_tool_turn(signature='sig-2', tool_id='toolu_02',
                                                                           path='b'))
    history = with_results(history, second, path='b')
    answer(app, fake, claude, history, events=anthropic_text('Done'))
    messages = last_body(fake)['messages']
    assert messages[1]['content'][0]['signature'] == 'sig-1'
    assert messages[3]['content'][0]['signature'] == 'sig-2'
    assert messages[3]['content'][-1]['id'] == 'toolu_02' and messages[4]['content'][0]['tool_use_id'] == 'toolu_02'
    assert last_body(fake)['thinking']['type'] == 'adaptive'


@pytest.mark.parametrize('edit', ['system', 'tools', 'earlier_result', 'model'])
def test_a_changed_prefix_drops_the_blocks_and_continues_without_thinking(app, fake, claude, edit):
    """Claude rejects a replayed block whose earlier conversation changed (preserved thinking)."""
    first = answer(app, fake, claude, [SYSTEM, USER])
    history = with_results([SYSTEM, USER], first)
    second = answer(app, fake, claude, history, events=thinking_tool_turn(signature='sig-2', tool_id='toolu_02',
                                                                           path='b'))
    history = with_results(history, second, path='b')
    tools, model = [TOOL], claude
    if edit == 'system':
        history[0] = {'role': 'system', 'content': 'A different system prompt.'}
    elif edit == 'tools':
        tools = [TOOL, {'type': 'function', 'function': {'name': 'write_file', 'parameters': {'type': 'object'}}}]
    elif edit == 'earlier_result':
        history[3] = {**history[3], 'content': '[output removed to save space]'}
    else:
        provider = make_provider(app, fake, protocol='anthropic', name='Other')
        model = make_model(app, provider, model='claude-other', tools=True, reasoning=['off', 'low', 'high'])
    answer(app, fake, model, history, events=anthropic_text('Done'), tools=tools)
    body = last_body(fake)
    blocks = [block['type'] for message in body['messages'] if isinstance(message['content'], list)
              for block in message['content']]
    assert 'thinking' not in blocks and 'redacted_thinking' not in blocks
    assert 'thinking' not in body and body['output_config'] == {'effort': 'high'}
    # The visible turns are sent exactly as before, with the provider's own tool ids.
    assert body['messages'][1]['content'] == VISIBLE
    assert body['messages'][3]['content'][-1]['id'] == 'toolu_02'


def test_a_new_user_message_starts_a_new_turn_without_earlier_blocks(app, fake, claude):
    first = answer(app, fake, claude, [SYSTEM, USER])
    history = [*with_results([SYSTEM, USER], first), {'role': 'user', 'content': 'Now the other file.'}]
    answer(app, fake, claude, history, events=anthropic_text('Done'))
    body = last_body(fake)
    assert body['messages'][1]['content'] == VISIBLE
    assert body['thinking']['type'] == 'adaptive'


def test_without_a_thinking_request_no_blocks_are_sent(app, fake, claude):
    first = answer(app, fake, claude, [SYSTEM, USER])
    answer(app, fake, claude, with_results([SYSTEM, USER], first), events=anthropic_text('Done'), effort='off')
    body = last_body(fake)
    assert body['messages'][1]['content'] == VISIBLE and 'thinking' not in body


def test_an_adaptive_turn_that_did_not_think_still_continues_with_thinking(app, fake, claude):
    first = answer(app, fake, claude, [SYSTEM, USER], events=thinking_tool_turn(thinking_blocks=False))
    answer(app, fake, claude, with_results([SYSTEM, USER], first), events=anthropic_text('Done'))
    body = last_body(fake)
    assert body['messages'][1]['content'] == VISIBLE
    assert body['thinking']['type'] == 'adaptive'


def test_a_budget_turn_without_thinking_blocks_continues_without_thinking(app, fake):
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider, model='claude-budget', tools=True, reasoning=['off', 'high'],
                       thinking='budget')
    first = answer(app, fake, model, [SYSTEM, USER], events=thinking_tool_turn(thinking_blocks=False))
    answer(app, fake, model, with_results([SYSTEM, USER], first), events=anthropic_text('Done'))
    assert 'thinking' not in last_body(fake)


def test_replayed_thinking_is_bounded(app, fake, claude, monkeypatch):
    monkeypatch.setattr(api, 'REPLAY_THINKING_CHARS', 10)
    first = answer(app, fake, claude, [SYSTEM, USER], events=thinking_tool_turn(thinking='x' * 50))
    answer(app, fake, claude, with_results([SYSTEM, USER], first), events=anthropic_text('Done'))
    body = last_body(fake)
    assert body['messages'][1]['content'][0]['type'] == 'text' and 'thinking' not in body


def test_a_record_that_does_not_match_the_tool_calls_is_not_used(app, fake, claude):
    first = answer(app, fake, claude, [SYSTEM, USER])
    history = with_results([SYSTEM, USER], first)
    history[2] = {**history[2], 'tool_calls': [{'function': {'name': 'write_file', 'arguments': {}}}]}
    history[3] = {**history[3], 'tool_name': 'write_file'}
    answer(app, fake, claude, history, events=anthropic_text('Done'))
    body = last_body(fake)
    assert [block['type'] for block in body['messages'][1]['content']] == ['text', 'tool_use']
    assert body['messages'][1]['content'][1]['id'].startswith('bc_call_') and 'thinking' not in body


def test_the_stream_records_the_turn_in_order_and_unknown_blocks_disable_it(app, fake, claude):
    final = answer(app, fake, claude, [SYSTEM, USER])
    record = final.extra['replay']
    assert record['content'] == SIGNED + VISIBLE and record['prefix']
    events = copy.deepcopy(thinking_tool_turn())
    events.insert(-2, {'type': 'content_block_start', 'index': 9,
                       'content_block': {'type': 'server_tool_use', 'id': 'srv_1', 'name': 'web_search'}})
    assert answer(app, fake, claude, [SYSTEM, USER], events=events).extra.get('replay') is None
    unsigned = [event for event in thinking_tool_turn() if event.get('delta', {}).get('type') != 'signature_delta']
    assert answer(app, fake, claude, [SYSTEM, USER], events=unsigned).extra.get('replay') is None


def test_openai_compatible_requests_ignore_the_record(app, fake, claude):
    first = answer(app, fake, claude, [SYSTEM, USER])
    history = with_results([SYSTEM, USER], first)
    provider = make_provider(app, fake, name='Compatible')
    model = make_model(app, provider, model='gpt-like', tools=True)
    fake.events = [{'choices': [{'delta': {'content': 'Done'}, 'finish_reason': 'stop'}]}]
    done(run(app, model, history, tools=[TOOL]))
    body = last_body(fake)
    assistant = body['messages'][2]
    assert 'replay' not in assistant and assistant['tool_calls'][0]['id'].startswith('bc_call_')
