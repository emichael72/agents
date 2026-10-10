"""
Module: test_agent.py

Description:
    Tests for the agent loop (`MCPAgent`), model profiles and shared instructions.

    A real MCP server runs the shared tools; the model is a small aiohttp server answering with
    scripted responses, so no model server or API key is needed.
"""
import asyncio
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

from mcpagent import REPO_ROOT
from mcpagent.client import MCPClient
from mcpagent.config import MCPAgentConfig
from mcpagent.agent import MCPAgent
from mcpagent.context import AgentContext
from mcpagent.output import Output
from mcpagent.profiles import ModelProfiles


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
        Write a config whose client starts the real MCP server on the shared tools (as the shipped
        one does), and connect an MCPAgent whose model requests are answered from `self.outputs`.
        """
        self.key_patch = patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'mcpagent.json'
        shipped = MCPAgentConfig.load().data
        self.config.write_text(json.dumps({"log_level": "ERROR", "tools_dir": shipped["tools_dir"], "tools_env": shipped["tools_env"],
            "servers": [{"server_id": "tools", "description": "Test shell tools", "transport": "STDIO"}]}))
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
        # OpenAI's error hints, as the OpenAI profile sets, though the stand-in server is local
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
        self.outputs = [[call(self.aliases['skill'], {"name": "pull-request"})],
                        [message('Follow its steps.')], [message('You asked about pull requests.')]]
        self.assertEqual(await self.agent.ask('How do I open a pull request?'), 'Follow its steps.')
        result = self.requests[1]['input'][-1]
        self.assertEqual(result['type'], 'function_call_output')
        self.assertIn('# Change code and open a pull request', result['output'])
        self.assertEqual(result['call_id'], 'call-1')
        self.assertFalse(self.requests[0]['store'])
        await self.agent.ask('What did I ask about?')
        self.assertIn('How do I open a pull request?', json.dumps(self.requests[2]['input']))
        self.assertNotIn('test-key-not-real', json.dumps(self.requests) + ''.join(self.traces))
        conn = self.agent.mcp._get_connection('tools')
        assert conn is not None
        self.assertIsNone(conn.session_id)
        self.assertEqual(conn.protocol_version, '2025-06-18')

    async def test_unknown_tool_and_bad_arguments_do_not_execute(self):
        for name, arguments in [('not_discovered', {}), (self.aliases['skill'], {'name': 123})]:
            self.outputs = [[call(name, arguments)], [message('Invalid tool call')]]
            await self.agent.ask('Try a tool')
            result = json.loads(self.requests[-1]['input'][-1]['output'])
            self.assertTrue(result['isError'])
        # Both calls are shown, then rejected before anything runs (no "←" result line)
        self.assertEqual(self.traces, [
            '→ not_discovered({})', '✗ not_discovered: The requested tool is not in the discovered tool list',
            '→ skill({"name":123})', "✗ skill: 123 is not of type 'string'",
        ])

    async def test_streaming_tool_loop_and_incremental_text(self):
        chunks = []
        requests = []
        alias = self.aliases['skill']

        async def stream(request):
            requests.append(await request.json())
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            if len(requests) == 1:
                output = [call(alias, {'name': 'pull-request'})]
            else:
                for text in ['Follow ', 'its steps.']:
                    event = {'type': 'response.output_text.delta', 'delta': text}
                    encoded = ('data: ' + json.dumps(event) + '\n\n').encode()
                    # Exercise events split across network chunks.
                    await response.write(encoded[:13])
                    await response.write(encoded[13:])
                output = [message('Follow its steps.')]
            event = {'type': 'response.completed', 'response': {'status': 'completed', 'output': output}}
            await response.write(('data: ' + json.dumps(event) + '\n\n').encode())
            await response.write_eof()
            return response

        self.model_handler = stream
        self.assertEqual(await self.agent.ask('How do I open a PR?', on_text=chunks.append), 'Follow its steps.')
        self.assertEqual(chunks, ['Follow ', 'its steps.'])
        self.assertTrue(all(request['stream'] for request in requests))
        self.assertIn('# Change code and open a pull request', requests[1]['input'][-1]['output'])
        self.assertEqual(self.agent.history[-1], message('Follow its steps.'))

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

    async def test_a_stream_error_says_what_the_server_reported(self):
        event = {"type": "error", "code": "model_unloaded", "message": "Model qwen-coder was unloaded (test-key-not-real)"}

        async def stream(request):
            self.requests.append(await request.json())
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            await response.write(f"data: {json.dumps(event)}\n\n".encode())
            return response

        self.model_handler = stream
        with self.assertRaisesRegex(RuntimeError, r'stream failed: model_unloaded\. Earlier tool') as caught:
            await self.agent.ask('Hello', on_text=lambda text: None)  # OpenAI's error hints: the code only
        self.assertNotIn('test-key-not-real', str(caught.exception))
        self.agent.local = True  # A local server: its own message too
        with self.assertRaisesRegex(RuntimeError, 'stream failed: model_unloaded Model qwen-coder was unloaded'):
            await self.agent.ask('Hello', on_text=lambda text: None)

    async def test_thinking_streams_to_the_spinner_and_max_tokens_is_named(self):
        self.agent.max_tokens, self.agent.profile = 150, 'local'
        events = [{"type": "response.reasoning_text.delta", "delta": "hmm"}] * 3
        completed = {"status": "completed", "output": [{"type": "reasoning", "content": []}],
                     "usage": {"input_tokens": 10, "output_tokens": 150}}  # LM Studio: only thinking, cut off

        async def stream(request):
            self.requests.append(await request.json())
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            for event in events + [{"type": "response.completed", "response": completed}]:
                await response.write(f"data: {json.dumps(event)}\n\n".encode())
            return response

        self.model_handler = stream
        thoughts = []
        with self.assertRaisesRegex(RuntimeError, r"^The model ran out of tokens: it may use 150 tokens per reply, "
                                                  r"thinking included \(max_tokens in the 'local' profile"):
            await self.agent.ask('Think hard', on_text=lambda text: None, on_thinking=thoughts.append)
        self.assertEqual(thoughts, ['hmm'] * 3)  # The thinking itself, for -d to save
        self.assertEqual(self.requests[0]['max_output_tokens'], 150)
        self.agent.max_tokens = None  # No limit: none is sent, and the reply is only "no text"
        with self.assertRaisesRegex(RuntimeError, 'returned no text or tool calls'):
            await self.agent.ask('Think hard', on_text=lambda text: None)
        self.assertNotIn('max_output_tokens', self.requests[-1])

    async def test_a_reply_out_of_tokens_is_asked_again_once(self):
        self.agent.max_tokens, self.agent.profile = 150, 'local'
        self.agent.out_of_tokens_retries, self.agent.out_of_tokens_prompt = 1, 'Think briefly.'
        self.agent.sampling = {'temperature': 0.6, 'top_k': 20}
        cut = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
               "output": [{"type": "reasoning", "content": []}], "usage": {"input_tokens": 10, "output_tokens": 150}}
        replies = [cut, {"status": "completed", "output": [message('Short answer')]}, cut, cut]

        async def answer(request):
            self.requests.append(await request.json())
            return web.json_response(replies.pop(0))

        self.model_handler = answer
        retried = []
        self.assertEqual(await self.agent.ask('Think hard', on_out_of_tokens=retried.append), 'Short answer')
        self.assertEqual(retried, [True])
        self.assertEqual(self.requests[1]['input'][-1], {'role': 'user', 'content': 'Think briefly.'})
        self.assertEqual((self.requests[0]['temperature'], self.requests[0]['top_k']), (0.6, 20))  # The profile's sampling
        with self.assertRaisesRegex(RuntimeError, '^The model ran out of tokens'):  # Asked again once only
            await self.agent.ask('Think harder', on_out_of_tokens=retried.append)
        self.assertEqual(retried, [True, True, False])

    async def test_tool_failure_returned_to_model(self):
        self.outputs = [[call(self.aliases['shell'], {'cwd': 'missing-file', 'command': 'ls'})],
                        [message('File not found')]]
        await self.agent.ask('Count missing-file')
        result = json.loads(self.requests[-1]['input'][-1]['output'])
        self.assertTrue(result['isError'])
        self.assertIn('not an allowed folder', result['content'][0]['text'])

    async def test_zero_means_no_tool_call_limit(self):
        self.agent.max_tool_calls = 0
        self.outputs = [[call(self.aliases['skill'], {'name': 'pull-request'}, f'call-{n}')] for n in range(4)] + [[message('Done')]]
        self.assertEqual(await self.agent.ask('Keep calling'), 'Done')  # Four calls, past the fixture's limit of 2

    async def test_call_limit_stops_repeated_execution(self):
        self.outputs = [[call(self.aliases['skill'], {'name': 'pull-request'}, f'call-{n}')] for n in range(3)]
        with patch.object(self.agent.mcp, 'request', wraps=self.agent.mcp.request) as request:
            with self.assertRaisesRegex(RuntimeError, 'limit'):
                await self.agent.ask('Keep calling')
            self.assertEqual(sum(c.args[0] == 'tools/call' for c in request.call_args_list), 2)
        self.assertEqual(self.agent.history, [])

    async def test_a_call_repeated_too_often_is_not_run(self):
        self.agent.max_tool_calls = 0  # The guard, not the call limit, must stop it
        self.agent.guard.limit = 2
        repeat = call(self.aliases['skill'], {'name': 'pull-request'})
        self.outputs = [[repeat], [repeat], [repeat], [message('Done.')], [repeat], [message('Done again.')]]
        with patch.object(self.agent.mcp, 'request', wraps=self.agent.mcp.request) as request:
            self.assertEqual(await self.agent.ask('Keep reading the skill'), 'Done.')
            self.assertEqual(sum(c.args[0] == 'tools/call' for c in request.call_args_list), 2)  # The third did not run
            refused = json.loads(self.requests[3]['input'][-1]['output'])
            self.assertTrue(refused['isError'])
            self.assertIn('Not run: you made this same skill call, with the same arguments, 2 times in a row',
                          refused['content'][0]['text'])
            await self.agent.ask('And now?')  # A new turn: the count starts again
            self.assertEqual(sum(c.args[0] == 'tools/call' for c in request.call_args_list), 3)

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

    async def test_the_agent_starts_its_server_and_stops_it(self):
        conn = self.agent.mcp._get_connection('tools')
        assert conn is not None and conn._proc is not None
        process = conn._proc
        self.assertIsNone(process.returncode)  # Started by connect, still running
        lines = []
        self.agent.mcp.on_server_output(lines.append)
        self.outputs = [[call(self.aliases['skill'], {'name': 'pull-request'})], [message('Done')]]
        await self.agent.ask('How do I open a PR?')
        for _ in range(50):  # The log line arrives on its own stream
            if lines:
                break
            await asyncio.sleep(0.02)
        self.assertRegex(lines[0], r'^ran skill: python3 skill/skill\.py --name=pull-request \(exit 0, \d+\.\ds\)$')
        await self.agent.close()
        self.assertEqual(process.returncode, 0)  # stdin closed: it exited by itself

    async def test_a_server_that_cannot_start_says_why(self):
        broken = Path(self.temp.name) / 'broken.json'
        broken.write_text(json.dumps({"log_level": "ERROR", "tools_dir": "no-such-folder",
            "servers": [{"server_id": "tools", "description": "Broken", "transport": "STDIO"}]}))
        client = MCPClient(broken)
        self.addAsyncCleanup(client.close, close_all=True)
        with self.assertRaisesRegex(EOFError, r"The MCP server 'tools' stopped \(exit 1\): error: .*no-such-folder"):
            await client.connect(connect_all=True)

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
        remote = Path(self.temp.name) / 'remote.json'  # Another MCP server, reached over HTTP
        remote.write_text(json.dumps({"log_level": "ERROR", "servers": [{
            "server_id": "tools", "description": "Remote tools", "transport": "HTTP",
            "config": {"url": str(server.make_url('/'))}}]}))
        client = MCPClient(remote)
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
        Load the settings of the config the package ships (jsons/mcpagent.json).
        Returns:
            dict: The parsed config.
        """
        return MCPAgentConfig.load().data

    async def test_model_profiles_come_from_the_shared_models_file(self):
        profiles = self.shipped_models()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key-not-real'}):
            for name in ('LOCAL_LLM_BASE_URL', 'LOCAL_LLM_MODEL', 'LOCAL_LLM_API_KEY'):
                os.environ.pop(name, None)
            with patch.object(ModelProfiles, 'loaded_model', return_value=None):  # No loaded model reported
                with self.assertRaisesRegex(ValueError, "No model is loaded on http://boba:1234/v1, and the 'local' "
                                                        "profile names none .*--model or LOCAL_LLM_MODEL"):
                    profiles.resolve()  # "default": "local", which never makes the server load a model
            with patch.object(ModelProfiles, 'loaded_model', return_value='qwen/loaded-now'):
                local = profiles.resolve()
            self.assertEqual((local['base_url'], local['model']), ('http://boba:1234/v1', 'qwen/loaded-now'))
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
        name = context.own()['name']  # From mcp/instructions.json
        self.assertEqual(name, 'mcp')
        self.assertTrue(context.identity(name).startswith('Your name is mcp.'))
        prompt = context.system_prompt(context.agent_settings())
        self.assertTrue(prompt.startswith('Your name is mcp.'))
        self.assertIn('\n\nYour tools run one at a time', prompt)  # Its own lines follow the shared ones
        self.assertEqual(AgentContext({}).own(), {})  # no agent_instructions_file configured

    def test_the_skills_join_the_instructions(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(AgentContext.skills_text(Path(folder)), '')  # No skills yet
            (Path(folder) / 'build').mkdir()
            (Path(folder) / 'build' / 'SKILL.md').write_text('---\nname: build\ndescription: Build and test a project.\n---\n\n# Build\n\n---\n\ndescription: not a header line\n')
            (Path(folder) / 'notes').mkdir()  # A folder without SKILL.md is not a skill
            text = AgentContext.skills_text(Path(folder))
            self.assertTrue(text.endswith('read it with the skill tool and follow it):\n- build: Build and test a project.'))
        self.assertEqual(AgentContext.skills_text(None), '')
        context = AgentContext(self.shipped_config())
        self.assertIn('\n- pull-request: ', context.system_prompt(context.agent_settings()))

    def test_exit_saves_only_after_a_tool_call_or_several_exchanges(self):
        ask, answer = {'role': 'user', 'content': 'hi'}, message('hello')
        self.assertFalse(AgentContext.worth_saving([]))
        self.assertFalse(AgentContext.worth_saving([ask, answer]))
        self.assertTrue(AgentContext.worth_saving([ask, answer, ask, answer]))
        self.assertTrue(AgentContext.worth_saving([ask, call('skill', {}), answer]))

    async def test_missing_key_and_unknown_profile(self):
        profiles = self.shipped_models()
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            with self.assertRaisesRegex(ValueError, 'OPENAI_API_KEY'):
                profiles.resolve('openai')
        with self.assertRaisesRegex(ValueError, "Unknown model profile 'nope'.*local.*openai"):
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
        settings = self.shipped_models().resolve('local', base_url=self.model_url, model='qwen/test')  # No LM Studio here
        local = MCPAgent(self.agent.mcp, base_url=settings['base_url'], model=settings['model'],
                           api_key=settings['api_key'], provider=settings['name'])
        local.tools = self.agent.tools
        try:
            self.assertEqual(await local.ask('Hello'), 'Hi')
        finally:
            if local._api is not None:
                await local._api.close()
        self.assertEqual(seen, [('/v1/responses', 'Bearer lm-studio')])  # The local profile's own key


# noinspection DuplicatedCode
class OutputTests(unittest.TestCase):
    """The terminal layout shared by the three agents (README.md, "Terminal output")."""

    def test_layout_settings_come_from_the_shared_context_file(self):
        settings = AgentContext(MCPAgentConfig.load().data).output_settings()
        self.assertEqual((settings['width'], settings['show_time']), (120, True))

    # The agents keep independent tests for their shared terminal behavior.
    # noinspection DuplicatedCode
    def test_with_debug_the_spinner_runs_between_the_lines_and_times_the_thinking(self):
        printed = io.StringIO()
        output = Output(Console(file=printed, force_terminal=True, width=120), {'width': 120, 'render': False}, debug=True)
        spinners = []
        output.out.status = Mock(side_effect=lambda *args, **kwargs: spinners.append(Mock()) or spinners[-1])
        now = [100.0]
        started = lambda: str(output.out.status.call_args.args[0])  # noqa: E731  A new spinner's label
        shown = lambda: str(spinners[-1].update.call_args.args[0])  # noqa: E731  Its label since
        with patch('mcpagent.output.time.monotonic', side_effect=lambda: now[0]):
            output.start()
            self.assertEqual(started(), 'Thinking…')  # A spinner with -d too
            output.thinking()
            now[0] = 103.4
            output.thinking()
            self.assertEqual(shown(), 'Thinking… 3s')  # A reasoning model's thinking, timed
            output.line('→ shell({"command":"ls"})')
            self.assertIn('→ shell({"command":"ls"})', printed.getvalue())  # The gray line still prints
            self.assertEqual(started(), 'Running shell…')
            output.line('← shell: a.c')
            self.assertEqual(started(), 'Thinking…')  # The model works on the result
            now[0] = 110.0
            output.thinking()
            now[0] = 112.5
            output.thinking()
            self.assertEqual(shown(), 'Thinking… 2s')  # A new stretch of thinking starts at 0
            output.text('Two files.')
            spinners[-1].stop.assert_called_once()  # The answer replaces the spinner
            output.thinking()
            self.assertEqual(len(spinners), 3)  # No spinner while the answer streams

    def test_the_answer_renders_as_markdown_block_by_block(self):
        printed = io.StringIO()
        output = Output(Console(file=printed, force_terminal=True, width=100, height=40), {'width': 100, 'show_time': False})
        output.start()
        answer = "## Title\n\nThe **flip** swaps `left`.\n\n```c\nint x = 1;\n```\n\n- one\n- two"
        for i in range(0, len(answer), 5):  # As a model streams it: a block is redrawn as it grows
            output.text(answer[i:i + 5])
        self.assertEqual(output.blocks, 3)  # Title, paragraph and code block are done; the list still streams
        output.finish()
        printed.seek(0)
        printed.truncate()
        output.start()
        for block in answer.split("\n\n"):  # Whole blocks: only finished frames, so no half-closed **
            output.text(block + "\n\n")
        output.finish()
        raw = printed.getvalue()
        plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw)
        for marker in ("**", "```", "## ", "- one"):
            self.assertNotIn(marker, plain)  # Markdown's markers are rendered, not shown
        self.assertIn("• one", plain)
        self.assertIn("\x1b[1m", raw)  # Bold
        self.assertIn("Title", plain)

    def test_blocks_end_at_a_blank_line_or_a_closed_code_block(self):
        self.assertEqual(Output.block_end("One.\n\nTwo"), len("One.\n\n"))
        self.assertIsNone(Output.block_end("One line, still streaming"))
        self.assertIsNone(Output.block_end("```c\nint x;\n\nint y;\n"))  # A blank line inside code does not end it
        self.assertEqual(Output.block_end("```c\nint x;\n```\nMore"), len("```c\nint x;\n```\n"))

    def test_with_debug_code_from_a_named_file_is_highlighted_dimmed(self):
        printed = io.StringIO()
        output = Output(Console(file=printed, force_terminal=True, width=100), {'width': 100}, debug=True)
        output.line('→ shell({"command":"cat -n src/pi.c"})')
        output.line("← shell:      1\t#include <math.h>\n     2\tint x = 1;")
        raw = printed.getvalue()
        dim = re.compile(r"\x1b\[(?:[0-9;]*;)?2(?:;[0-9;]*)?m")  # An SGR code with 2, dim (not 2K, erase)
        self.assertRegex(raw, dim)
        self.assertIn("int", re.sub(r"\x1b\[[0-9;]*m", "", raw))
        printed.seek(0)
        printed.truncate()
        output.line('→ shell({"command":"ls"})')  # No file named: a plain gray line
        output.line("← shell: a.c\nb.c")
        self.assertNotRegex(printed.getvalue(), dim)

    def test_piped_output_and_plain_do_not_render(self):
        self.assertFalse(Output(Console(file=io.StringIO()), {'width': 100}).render)  # Not a terminal
        self.assertFalse(Output(Console(file=io.StringIO(), force_terminal=True), {'render': False}).render)
        self.assertTrue(Output(Console(file=io.StringIO(), force_terminal=True), {'width': 100}).render)

    def test_by_default_only_the_answer_and_timing_print_and_tools_show_on_the_spinner(self):
        printed = io.StringIO()
        output = Output(Console(file=printed, force_terminal=True, width=120), {"width": 120, "show_time": True, "render": False},
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
        output = Output(Console(file=printed, force_terminal=True, width=120), {"width": 120, "links": True, "render": False})
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
