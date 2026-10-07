"""
Module: test_agent.py

Description:
    Tests for the agent loop (`MCPAgent`), model profiles and shared instructions.

    A real MCP server runs the shared tools; the model is a small aiohttp server answering with
    scripted responses, so no model server or API key is needed.
"""
from pathlib import Path
import json
import re
import os
import tempfile
import unittest
from collections.abc import Awaitable, Callable
from unittest.mock import Mock, patch

import io

from aiohttp import web
from aiohttp.test_utils import TestServer
from rich.console import Console

from mcpagent import MCPClient, MCPService
from mcpagent.config import REPO_ROOT, MCPAgentConfig
from mcpagent.client.agent import MCPAgent
from mcpagent.client.context import AgentContext
from mcpagent.client.output import Output
from mcpagent.client.profiles import ModelProfiles


# The scripted turn every agent's tests replay
SCENARIO = json.loads((REPO_ROOT / 'tests' / 'scenario.json').read_text())

def message(text):
    """
    Build an assistant message item, as the Responses API returns it.
    Args:
        text: The message text.
    Returns:
        dict: A "message" output item.
    """
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def call(name, arguments, call_id="call-1"):
    """
    Build a function_call item, as the Responses API returns it.
    Args:
        name: The tool alias the model calls.
        arguments: The call's arguments.
        call_id: The call id that the tool result must reference.
    Returns:
        dict: A "function_call" output item.
    """
    return {"type": "function_call", "call_id": call_id, "name": name,
            "arguments": json.dumps(arguments)}


class MCPAgentTests(unittest.IsolatedAsyncioTestCase):
    """Agent-loop tests against a real MCP server, with scripted model responses."""
    model_handler: Callable[[web.Request], Awaitable[web.StreamResponse]]

    async def asyncSetUp(self):
        """
        Start an MCP server on the shared tools, write a client config for it, and connect an
        MCPAgent whose model requests are answered from `self.outputs`.
        """
        self.key_patch = patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        old = Path.cwd()
        try:
            os.chdir(REPO_ROOT)  # As MCPService.serve does: config paths are repository-relative
            self.service = MCPService(MCPAgentConfig.load().server)
        finally:
            os.chdir(old)
        self.server = TestServer(self.service._app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'mcpagent.json'
        self.config.write_text(json.dumps({"client": {"log_level": "ERROR", "servers": [{
            "server_id": "tools", "description": "Test shell tools", "transport": "HTTP", "config": {"url": str(self.server.make_url('/'))}
        }]}}))
        self.requests = []
        self.outputs = []
        self.traces = []
        self.status = 200
        self.error_code = None

        # A test can replace how the model server answers.
        self.model_handler = self._model_response

        async def responses(request: web.Request) -> web.StreamResponse:
            return await self.model_handler(request)

        model_app = web.Application()
        model_app.router.add_post('/v1/responses', responses)
        self.model_server = TestServer(model_app)
        await self.model_server.start_server()
        self.addAsyncCleanup(self.model_server.close)
        self.model_url = str(self.model_server.make_url('/v1'))
        # OpenAI's error hints, as the openai profile sets, though the stand-in server is local
        self.agent = MCPAgent(MCPClient(self.config), base_url=self.model_url, model='gpt-4.1-mini',
                               api_key='test-key-not-real', error_hints='openai', trace=self.traces.append,
                               max_tool_calls=2)
        self.addAsyncCleanup(self.agent.close)
        await self.agent.connect()
        self.aliases = {name: alias for alias, (_, name, _) in self.agent.routes.items()}

    async def _model_response(self, request: web.Request) -> web.StreamResponse:
        """Answer a model request using the test's scripted output."""
        self.assertEqual(request.path, '/v1/responses')
        self.assertEqual(request.headers['Authorization'], 'Bearer test-key-not-real')
        self.requests.append(await request.json())
        if self.status != 200:
            return web.json_response({"error": {"message": "test-key-not-real", "code": self.error_code}},
                                     status=self.status)
        return web.json_response({"status": "completed", "output": self.outputs.pop(0)})

    async def test_shared_scenario(self):
        # The scripted turn every agent's tests replay: all calls in one response, the real tools run
        self.agent.max_tool_calls = 0
        steps = SCENARIO['calls']
        self.outputs = [[call(self.aliases[step['tool']], step['arguments'], f'call-{n}') for n, step in enumerate(steps)],
                        [message('Checked')]]
        self.assertEqual(await self.agent.ask(SCENARIO['prompt']), 'Checked')
        results = [item for item in self.requests[1]['input'] if item.get('type') == 'function_call_output']
        self.assertEqual([item['call_id'] for item in results], [f'call-{n}' for n in range(len(steps))])
        for n, step in enumerate(steps):
            # Tool lines use the real tool name (not the alias), the arguments and the readable output
            self.assertEqual(self.traces[2 * n], f"→ {step['tool']}({json.dumps(step['arguments'], separators=(',', ':'))})")
            mark = '←' if step['outcome'] == 'ok' else '✗'
            self.assertTrue(self.traces[2 * n + 1].startswith(f"{mark} {step['tool']}: "), self.traces[2 * n + 1])
            self.assertIn(step['output'], self.traces[2 * n + 1])
            self.assertIn(step['output'], results[n]['output'])

    async def test_real_tool_and_followup_history(self):
        self.outputs = [[call(self.aliases['time'], {"timezone": "UTC"})],
                        [message('It is noon in UTC.')], [message('You asked about UTC.')]]
        self.assertEqual(await self.agent.ask('What time is it in UTC?'), 'It is noon in UTC.')
        result = self.requests[1]['input'][-1]
        self.assertEqual(result['type'], 'function_call_output')
        self.assertIn('(UTC+00:00)', result['output'])
        self.assertEqual(result['call_id'], 'call-1')
        self.assertFalse(self.requests[0]['store'])
        await self.agent.ask('Which time zone did I use?')
        self.assertIn('What time is it in UTC?', json.dumps(self.requests[2]['input']))
        self.assertNotIn('test-key-not-real', json.dumps(self.requests) + ''.join(self.traces))
        conn = self.agent.mcp._get_connection('tools')
        assert conn is not None
        self.assertIsNone(conn.session_id)
        self.assertEqual(conn.protocol_version, '2025-06-18')

    async def test_unknown_tool_and_bad_arguments_do_not_execute(self):
        for name, arguments in [('not_discovered', {}), (self.aliases['time'], {'timezone': 123})]:
            self.outputs = [[call(name, arguments)], [message('Invalid tool call')]]
            await self.agent.ask('Try a tool')
            result = json.loads(self.requests[-1]['input'][-1]['output'])
            self.assertTrue(result['isError'])
        # Both calls are shown, then rejected before anything runs (no "←" result line)
        self.assertEqual(self.traces, [
            '→ not_discovered({})', '✗ not_discovered: The requested tool is not in the discovered tool list',
            '→ time({"timezone":123})', "✗ time: 123 is not of type 'string'",
        ])

    async def test_streaming_tool_loop_and_incremental_text(self):
        chunks = []
        requests = []
        alias = self.aliases['time']

        async def stream(request):
            requests.append(await request.json())
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            if len(requests) == 1:
                output = [call(alias, {'timezone': 'UTC'})]
            else:
                for text in ['It is ', 'noon.']:
                    event = {'type': 'response.output_text.delta', 'delta': text}
                    encoded = ('data: ' + json.dumps(event) + '\n\n').encode()
                    # Exercise events split across network chunks.
                    await response.write(encoded[:13])
                    await response.write(encoded[13:])
                output = [message('It is noon.')]
            event = {'type': 'response.completed', 'response': {'status': 'completed', 'output': output}}
            await response.write(('data: ' + json.dumps(event) + '\n\n').encode())
            await response.write_eof()
            return response

        self.model_handler = stream
        self.assertEqual(await self.agent.ask('Time in UTC?', on_text=chunks.append), 'It is noon.')
        self.assertEqual(chunks, ['It is ', 'noon.'])
        self.assertTrue(all(request['stream'] for request in requests))
        self.assertIn('(UTC+00:00)', requests[1]['input'][-1]['output'])
        self.assertEqual(self.agent.history[-1], message('It is noon.'))

    async def test_interrupted_stream_clears_history_without_retry(self):
        requests = []

        async def cut_short(request):  # A stream that ends without its completed event
            requests.append(request)
            return web.Response(text='data: {"type":"response.output_text.delta","delta":"Hi"}\n\n',
                                content_type='text/event-stream')

        self.model_handler = cut_short
        self.agent.history = [message('Earlier answer')]
        chunks = []
        with self.assertRaisesRegex(RuntimeError, 'before completion'):
            await self.agent.ask('Hello', on_text=chunks.append)
        self.assertEqual(chunks, ['Hi'])
        self.assertEqual(self.agent.history, [])
        self.assertEqual(len(requests), 1)

    async def test_streaming_http_error_is_redacted(self):
        self.status = 401
        with self.assertRaisesRegex(RuntimeError, 'OPENAI_API_KEY') as caught:
            await self.agent.ask('Hello', on_text=lambda text: None)
        self.assertNotIn('test-key-not-real', str(caught.exception))
        self.assertEqual(len(self.requests), 1)

    async def test_tool_failure_returned_to_model(self):
        self.outputs = [[call(self.aliases['shell'], {'cwd': 'missing-file', 'command': 'ls'})],
                        [message('File not found')]]
        await self.agent.ask('Count missing-file')
        result = json.loads(self.requests[-1]['input'][-1]['output'])
        self.assertTrue(result['isError'])
        self.assertIn('not an allowed folder', result['content'][0]['text'])

    async def test_zero_means_no_tool_call_limit(self):
        self.agent.max_tool_calls = 0
        self.outputs = [[call(self.aliases['time'], {'timezone': 'UTC'}, f'call-{n}')] for n in range(4)] + [[message('Done')]]
        self.assertEqual(await self.agent.ask('Keep calling'), 'Done')  # Four calls, past the fixture's limit of 2

    async def test_call_limit_stops_repeated_execution(self):
        self.outputs = [[call(self.aliases['time'], {'timezone': 'UTC'}, f'call-{n}')] for n in range(3)]
        with patch.object(self.agent.mcp, 'request', wraps=self.agent.mcp.request) as request:
            with self.assertRaisesRegex(RuntimeError, 'limit'):
                await self.agent.ask('Keep calling')
            self.assertEqual(sum(c.args[0] == 'tools/call' for c in request.call_args_list), 2)
        self.assertEqual(self.agent.history, [])

    async def test_api_error_redacts_body_and_does_not_retry(self):
        self.status = 401
        with self.assertRaisesRegex(RuntimeError, 'OPENAI_API_KEY') as caught:
            await self.agent.ask('Hello')
        self.assertNotIn('test-key-not-real', str(caught.exception))
        self.assertEqual(len(self.requests), 1)

    async def test_quota_error_is_actionable(self):
        self.status = 429
        self.error_code = "insufficient_quota"
        with self.assertRaisesRegex(RuntimeError, 'insufficient_quota'):
            await self.agent.ask('Hello')

    async def test_stateful_mcp_session_header_and_notification(self):
        received = []

        async def rpc(request):
            payload = await request.json()
            received.append((payload, dict(request.headers)))
            self.assertNotIn("Authorization", request.headers)
            if payload["method"] == "initialize":
                self.assertIn("clientInfo", payload["params"])
                return web.json_response({"jsonrpc": "2.0", "id": payload["id"],
                    "result": {"protocolVersion": "2025-06-18"}},
                    headers={"Mcp-Session-Id": "session-not-request-id"})
            self.assertEqual(request.headers["Mcp-Session-Id"], "session-not-request-id")
            self.assertEqual(request.headers["MCP-Protocol-Version"], "2025-06-18")
            self.assertNotIn("id", payload)
            self.assertEqual(payload["method"], "notifications/initialized")
            return web.Response(status=202)

        app = web.Application()
        app.router.add_post('/', rpc)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        config = json.loads(self.config.read_text())
        config['client']['servers'][0]['config']['url'] = str(server.make_url('/'))
        self.config.write_text(json.dumps(config))
        client = MCPClient(self.config)
        self.addAsyncCleanup(client.close, close_all=True)
        await client.connect(connect_all=True)
        self.assertEqual(len(received), 2)
        conn = client._get_connection('tools')
        assert conn is not None
        self.assertEqual(conn.session_id, 'session-not-request-id')

    def shipped_models(self):
        """
        Load the shared model profiles through the shipped jsons/mcpagent.json.
        Returns:
            ModelProfiles: The profiles.
        """
        return ModelProfiles.load(self.shipped_config())

    @staticmethod
    def shipped_config():
        """
        Load the client section of the config the package ships (jsons/mcpagent.json).
        Returns:
            dict: The parsed config.
        """
        return MCPAgentConfig.load().data['client']

    async def test_model_profiles_come_from_the_shared_models_file(self):
        profiles = self.shipped_models()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key-not-real'}):
            for name in ('LOCAL_LLM_BASE_URL', 'LOCAL_LLM_MODEL', 'LOCAL_LLM_API_KEY'):
                os.environ.pop(name, None)
            with patch.object(ModelProfiles, 'loaded_model', return_value=None):  # No loaded model reported
                local = profiles.resolve()  # "default": "local"
            fallback = profiles.models['profiles']['local']  # Whatever the file names, so editing it never breaks this test
            self.assertEqual((local['base_url'], local['model']), (fallback['base_url'], fallback['model']))
            with patch.object(ModelProfiles, 'loaded_model', return_value='qwen/loaded-now'):
                self.assertEqual(profiles.resolve()['model'], 'qwen/loaded-now')  # model_auto
                self.assertEqual(profiles.resolve(model='explicit')['model'], 'explicit')
            self.assertEqual(local['api_key'], 'lm-studio')  # the OpenAI key is never used for another server
            os.environ['LOCAL_LLM_MODEL'] = 'from-env'
            self.assertEqual(profiles.resolve('local')['model'], 'from-env')
            self.assertEqual(profiles.resolve('local', model='from-cli')['model'], 'from-cli')
            self.assertEqual(profiles.resolve('openai')['api_key'], 'test-key-not-real')
            self.assertEqual(profiles.resolve('openai')['error_hints'], 'openai')
            self.assertIsNone(local['error_hints'])  # A local server gets the generic error advice

    async def test_instructions_come_from_the_shared_context_file(self):
        context = AgentContext(self.shipped_config())
        instructions = context.instructions()
        self.assertTrue(instructions.startswith('You are an agent'))
        self.assertIn('allowed folder', instructions)
        self.assertEqual(AgentContext({}).instructions(), '')  # no instructions_file configured
        self.assertIn('Nothing to save', context.instructions('on_exit'))
        name = context.agent_settings()['names'][AgentContext.NAME_KEY]
        self.assertEqual(name, 'mcp')
        self.assertTrue(context.identity(name).startswith('Your name is mcp.'))
        self.assertTrue(context.system_prompt(context.agent_settings()).startswith('Your name is mcp.'))

    def test_exit_saves_only_after_a_tool_call_or_several_exchanges(self):
        ask, answer = {'role': 'user', 'content': 'hi'}, message('hello')
        self.assertFalse(AgentContext.worth_saving([]))
        self.assertFalse(AgentContext.worth_saving([ask, answer]))
        self.assertTrue(AgentContext.worth_saving([ask, answer, ask, answer]))
        self.assertTrue(AgentContext.worth_saving([ask, call('time', {}), answer]))

    async def test_missing_key_and_unknown_profile(self):
        profiles = self.shipped_models()
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            with self.assertRaisesRegex(ValueError, 'OPENAI_API_KEY'):
                profiles.resolve('openai')
        with self.assertRaisesRegex(ValueError, "Unknown model profile 'nope'.*local, openai"):
            profiles.resolve('nope')
        with self.assertRaisesRegex(ValueError, "missing model"):
            ModelProfiles({'profiles': {'broken': {'base_url': 'http://x', 'api_key': 'k'}}}).resolve('broken')
        with self.assertRaisesRegex(ValueError, 'models_file'):
            ModelProfiles.load({})

    async def test_local_profile_sends_its_own_key_to_its_own_server(self):
        seen = []

        async def respond(request):
            seen.append((request.path, request.headers['Authorization']))
            return web.json_response({"status": "completed", "output": [message('Hi')]})

        self.model_handler = respond
        settings = self.shipped_models().resolve('local', base_url=self.model_url)
        local = MCPAgent(self.agent.mcp, base_url=settings['base_url'], model=settings['model'],
                           api_key=settings['api_key'], provider=settings['name'])
        local.tools = self.agent.tools
        try:
            self.assertEqual(await local.ask('Hello'), 'Hi')
        finally:
            if local._api is not None:
                await local._api.close()
        self.assertEqual(seen, [('/v1/responses', 'Bearer lm-studio')])  # The local profile's own key


class OutputTests(unittest.TestCase):
    """The terminal layout shared by the three agents (README.md, "Terminal output")."""

    def test_layout_settings_come_from_the_shared_context_file(self):
        settings = AgentContext(MCPAgentConfig.load().data['client']).output_settings()
        self.assertEqual((settings['width'], settings['show_time']), (120, True))

    # The agents keep independent tests for their shared terminal behavior.
    # noinspection DuplicatedCode
    def test_by_default_only_the_answer_and_timing_print_and_tools_show_on_the_spinner(self):
        printed = io.StringIO()
        output = Output(Console(file=printed, force_terminal=True, width=120), {"width": 120, "show_time": True},
                        debug=False)
        spinner = Mock()
        output.out.status = Mock(return_value=spinner)  # rich's spinner, without drawing it
        output.start()
        label = lambda: str(spinner.update.call_args.args[0])  # noqa: E731
        output.line('→ shell({"command":"ls"})')
        self.assertEqual(label(), "Running shell…")
        output.line("← shell: a.c")
        self.assertEqual(label(), "Thinking…")
        output.text("Two files.")
        spinner.stop.assert_called_once()  # The answer replaces the spinner
        output.line('→ ed({"path":"a.c"})')  # A call after the answer text: the spinner comes back
        self.assertEqual(output.out.status.call_count, 2)
        self.assertEqual(str(output.out.status.call_args.args[0]), "Running ed…")
        output.line("banner or hint")
        output.finish()
        output.note("History cleared.")
        plain = re.sub(r"\x1b\[[0-9;]*m", "", printed.getvalue())
        self.assertEqual(plain.splitlines()[1:], ["Two files.", "Response time: 0.0s", "", "History cleared."])

    # noinspection DuplicatedCode
    def test_text_around_hidden_tool_calls_has_one_blank_line_between(self):
        for more_text in (True, False):
            printed = io.StringIO()
            output = Output(Console(file=printed), {"width": 120, "show_time": True}, debug=False)
            output.start()
            output.text("Let me check the build.")
            output.line('→ shell({"command":"make"})')
            output.line("← shell: ok")
            if more_text:
                output.text("It builds cleanly.")
            output.finish()
            expected = ["", "Let me check the build."] + (["", "It builds cleanly."] if more_text else [])
            self.assertEqual(printed.getvalue().splitlines(), expected + ["Response time: 0.0s", ""])

    def test_a_link_in_a_gray_line_is_bright_cyan_and_the_rest_stays_gray(self):
        printed = io.StringIO()
        output = Output(Console(file=printed, force_terminal=True, width=120), {"width": 120, "links": True})
        output.line("← pr: quiz at http://minion:8000/q/abc for PR #12")
        raw = printed.getvalue()
        self.assertRegex(raw, r"\x1b\[90m← pr: quiz at ")  # Gray before the link
        self.assertIn("\x1b]8;", raw)  # Clickable
        self.assertIn("\x1b[96mhttp://minion:8000/q/abc", raw)  # The link, bright cyan
        self.assertRegex(raw, r"\x1b\[90m for PR #12")  # Gray again after it

    def test_token_counts_follow_the_response_time(self):
        printed = io.StringIO()
        output = Output(Console(file=printed), {'width': 120, 'show_time': True, 'show_tokens': True})
        output.add_usage(1200, 34)
        output.finish()
        self.assertRegex(printed.getvalue(), r'Response time: \d+\.\ds · tokens: 1,200 in, 34 out · 1 model call\n\n$')

    def test_lines_and_streamed_answer_wrap_and_the_response_is_timed(self):
        printed = io.StringIO()
        output = Output(Console(file=printed), {'width': 30, 'show_time': True})
        self.assertEqual(Output.wrap('one two three four five six seven', 15), ['one two three', '  four five six', '  seven'])
        answer = 'The quiz service is running and pull request number one is still waiting for its quiz.'
        for i in range(0, len(answer), 7):
            output.text(answer[i:i + 7])
        output.finish()
        body = [line for line in printed.getvalue().split('\n') if line and not line.startswith('Response time')]
        self.assertTrue(all(len(line) <= 30 for line in body))
        self.assertEqual(' '.join(body), answer)
        self.assertRegex(printed.getvalue(), r'[^\n]\nResponse time: \d+\.\ds\n\n$')


if __name__ == '__main__':
    unittest.main()
