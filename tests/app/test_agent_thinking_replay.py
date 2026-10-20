"""An agent on a Claude model keeps extended thinking through its tool rounds.

The fake Anthropic server answers each request from a script: a signed
thinking block and a tool call, then (after the tool ran in the fake sandbox)
another one that calls ``finish``. The second request must carry the first
turn unchanged, signed thinking first, and ask for thinking again.
"""
from __future__ import annotations

from tests.app.test_agents import (  # noqa: F401 - fixtures
    add_user, agents_app, fast_loop, runner, start_ok, steps, token_file, wait_status,
)
from tests.app.conftest import Browser
from tests.app.test_provider_dialects import FakeProvider, make_model, make_provider


class ScriptedProvider(FakeProvider):
    """Streams ``script[n]`` for the n-th POST."""

    def __init__(self, script):
        self.script = list(script)
        super().__init__()

    @property
    def events(self):
        return self.script.pop(0) if self.script else []

    @events.setter
    def events(self, value):
        pass


def signed_tool_turn(signature, tool, arguments, tool_id):
    return [
        {'type': 'message_start', 'message': {'usage': {'input_tokens': 50, 'output_tokens': 1}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta',
                                                              'thinking': f'Thinking before {tool}.'}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': signature}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'content_block_start', 'index': 1,
         'content_block': {'type': 'tool_use', 'id': tool_id, 'name': tool, 'input': {}}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta',
                                                              'partial_json': arguments}},
        {'type': 'content_block_stop', 'index': 1},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 30}},
        {'type': 'message_stop'}]


def test_agent_tool_rounds_send_signed_thinking_back_and_keep_thinking(agents_app, runner):
    fake = ScriptedProvider([
        signed_tool_turn('sig-bash', 'bash', '{"command": "echo hi"}', 'toolu_bash'),
        signed_tool_turn('sig-finish', 'finish', '{"summary": "Said hi."}', 'toolu_finish'),
    ])
    try:
        app = agents_app()
        provider = make_provider(app, fake, protocol='anthropic')
        model = make_model(app, provider, model='claude-agent', tools=True, reasoning=['off', 'low', 'high'])
        alice = add_user(app, 'alice', allowed=True)
        browser = Browser(app)
        browser.login('alice')
        runner.exec_handler = lambda box, command, timeout: {'stdout': 'hi\n'}
        task_id = start_ok(browser, 'Say hi', model=model['ollama_name'])
        row = wait_status(app, task_id, 'finished')
        assert row['summary'] == 'Said hi.' and alice['id']
        first, second = fake.posts()
        assert first['body']['thinking']['type'] == 'adaptive'
        turn = second['body']['messages'][1]
        assert turn['role'] == 'assistant'
        assert turn['content'][0] == {'type': 'thinking', 'thinking': 'Thinking before bash.',
                                      'signature': 'sig-bash'}
        assert turn['content'][1] == {'type': 'tool_use', 'id': 'toolu_bash', 'name': 'bash',
                                      'input': {'command': 'echo hi'}}
        assert second['body']['messages'][2]['content'][0]['tool_use_id'] == 'toolu_bash'
        assert second['body']['thinking']['type'] == 'adaptive'
        # The timeline still shows each step's reasoning; signatures are never stored.
        log = steps(app, task_id)
        assert [step['thinking'] for step in log if step['kind'] == 'assistant'] == [
            'Thinking before bash.', 'Thinking before finish.']
        assert not any('sig-' in str(value) for step in log for value in step.values())
    finally:
        fake.close()


def tool_turn(*calls):
    events = [{'type': 'message_start', 'message': {'usage': {'input_tokens': 50, 'output_tokens': 1}}}]
    for index, (tool, arguments, tool_id) in enumerate(calls):
        events += [
            {'type': 'content_block_start', 'index': index,
             'content_block': {'type': 'tool_use', 'id': tool_id, 'name': tool, 'input': {}}},
            {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'input_json_delta',
                                                                      'partial_json': arguments}},
            {'type': 'content_block_stop', 'index': index}]
    return events + [{'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 30}},
                     {'type': 'message_stop'}]


def test_a_follow_up_answers_the_calls_a_finished_step_never_ran(agents_app, runner):
    fake = ScriptedProvider([
        tool_turn(('finish', '{"summary": "Said hi."}', 'toolu_finish'), ('bash', '{"command": "ls"}', 'toolu_bash')),
        tool_turn(('finish', '{"summary": "Listed."}', 'toolu_again')),
    ])
    try:
        app = agents_app()
        provider = make_provider(app, fake, protocol='anthropic')
        model = make_model(app, provider, model='claude-agent', tools=True)
        add_user(app, 'alice', allowed=True)
        browser = Browser(app)
        browser.login('alice')
        task_id = start_ok(browser, 'Say hi', model=model['ollama_name'])
        assert wait_status(app, task_id, 'finished')['summary'] == 'Said hi.'
        assert browser.post_json(f'/agents/{task_id}/messages', {'content': 'Now list the files'}).status_code == 202
        assert wait_status(app, task_id, 'finished')['summary'] == 'Listed.'
        assert not runner.execs
        messages = fake.posts()[-1]['body']['messages']
        calls = next(index for index, item in enumerate(messages) if item['role'] == 'assistant')
        ids = [block['id'] for block in messages[calls]['content']]
        results = messages[calls + 1]['content']
        assert [block['tool_use_id'] for block in results] == ids
        assert results[1]['content'] == 'Not run: the run stopped before this call.'
        assert messages[-1] == {'role': 'user', 'content': 'Now list the files'}
    finally:
        fake.close()
