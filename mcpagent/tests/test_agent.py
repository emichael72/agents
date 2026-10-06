"""
Module: test_agent.py

Description:
    Tests for the agent loop (`MCPAgent`), model profiles and shared instructions.

    A real MCP server runs the shared tools; the model is replaced by scripted responses
    through `httpx.MockTransport`, so no model server or API key is needed.
"""
import sys
from pathlib import Path

# Run from any folder: the repository root holds the mcpagent package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import io

import httpx
import json5
from aiohttp import web
from aiohttp.test_utils import TestServer
from rich.console import Console

from mcpagent import MCPClient, MCPService
from mcpagent.client.client import DEFAULT_CONFIG as CLIENT_CONFIG
from mcpagent.server.service import DEFAULT_CONFIG as SERVER_CONFIG
from mcpagent.client.agent import (MCPAgent, Output, load_instructions, load_models, load_output_settings,
                                   resolve_model, wrap)


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
    async def asyncSetUp(self):
        """
        Start an MCP server on the shared tools, write a client config for it, and connect an
        MCPAgent whose model requests are answered from `self.outputs`.
        """
        self.key_patch = patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        config = SERVER_CONFIG
        old = Path.cwd()
        try:
            os.chdir(config.parent)
            self.service = MCPService(json5.loads(config.read_text()))
        finally:
            os.chdir(old)
        self.server = TestServer(self.service._app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'client.json'
        self.config.write_text(json.dumps({"log_level": "ERROR", "servers": [{
            "server_id": "tools", "description": "Test shell tools", "transport": "HTTP", "config": {"url": str(self.server.make_url('/'))}
        }]}))
        self.requests = []
        self.outputs = []
        self.traces = []
        self.status = 200
        self.error_code = None

        def respond(request):
            self.assertEqual(str(request.url), 'https://api.openai.com/v1/responses')
            self.assertEqual(request.headers['Authorization'], 'Bearer test-key-not-real')
            self.requests.append(json.loads(request.content))
            if self.status != 200:
                return httpx.Response(self.status, json={"error": {"message": "test-key-not-real", "code": self.error_code}})
            return httpx.Response(200, json={"status": "completed", "output": self.outputs.pop(0)})

        self.agent = MCPAgent(MCPClient(self.config), base_url='https://api.openai.com/v1', model='gpt-4.1-mini',
                               api_key='test-key-not-real', trace=self.traces.append,
                               api_transport=httpx.MockTransport(respond), max_tool_calls=2)
        self.addAsyncCleanup(self.agent.close)
        await self.agent.connect()
        self.aliases = {name: alias for alias, (_, name, _) in self.agent.routes.items()}

    async def test_real_shell_tool_and_followup_history(self):
        self.outputs = [[call(self.aliases['greet'], {"name": "Alice Smith"})],
                        [message('Hello, Alice Smith!')], [message('The name was Alice Smith.')]]
        self.assertEqual(await self.agent.ask('Greet Alice Smith'), 'Hello, Alice Smith!')
        result = self.requests[1]['input'][-1]
        self.assertEqual(result['type'], 'function_call_output')
        self.assertIn('Hello, Alice Smith!', result['output'])
        self.assertEqual(result['call_id'], 'call-1')
        self.assertFalse(self.requests[0]['store'])
        # Tool lines use the real tool name (not the alias) and the readable output
        self.assertEqual(self.traces[:2], ['→ greet({"name":"Alice Smith"})',
                                           '← greet: Hello, Alice Smith! Greetings from the MCP Agent.'])
        await self.agent.ask('What name did I use?')
        self.assertIn('Greet Alice Smith', json.dumps(self.requests[2]['input']))
        self.assertNotIn('test-key-not-real', json.dumps(self.requests) + ''.join(self.traces))
        conn = self.agent.mcp._get_connection('tools')
        assert conn is not None
        self.assertIsNone(conn.session_id)
        self.assertEqual(conn.protocol_version, '2025-06-18')

    async def test_unknown_tool_and_bad_arguments_do_not_execute(self):
        for name, arguments in [('not_discovered', {}), (self.aliases['greet'], {'name': 123})]:
            self.outputs = [[call(name, arguments)], [message('Invalid tool call')]]
            await self.agent.ask('Try a tool')
            result = json.loads(self.requests[-1]['input'][-1]['output'])
            self.assertTrue(result['isError'])
        # Both calls are shown, then rejected before anything runs (no "←" result line)
        self.assertEqual(self.traces, [
            '→ not_discovered({})', '✗ not_discovered: The requested tool is not in the discovered tool list',
            '→ greet({"name":123})', "✗ greet: 123 is not of type 'string'",
        ])

    async def test_streaming_tool_loop_and_incremental_text(self):
        chunks = []
        requests = []
        alias = self.aliases['greet']

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                if len(requests) == 1:
                    output = [call(alias, {'name': 'Alice'})]
                else:
                    for text in ['Hello, ', 'Alice!']:
                        event = {'type': 'response.output_text.delta', 'delta': text}
                        encoded = ('data: ' + json.dumps(event) + '\n\n').encode()
                        # Exercise events split across network chunks.
                        yield encoded[:13]
                        yield encoded[13:]
                        assert chunks[-1] == text
                    output = [message('Hello, Alice!')]
                event = {'type': 'response.completed',
                         'response': {'status': 'completed', 'output': output}}
                yield ('data: ' + json.dumps(event) + '\n\n').encode()

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, stream=Stream())

        await self.agent.api.aclose()
        self.agent.api = httpx.AsyncClient(base_url='https://api.openai.com/v1/',
                                         transport=httpx.MockTransport(respond))
        self.assertEqual(await self.agent.ask('Greet Alice', on_text=chunks.append), 'Hello, Alice!')
        self.assertEqual(chunks, ['Hello, ', 'Alice!'])
        self.assertTrue(all(request['stream'] for request in requests))
        self.assertIn('Hello, Alice!', requests[1]['input'][-1]['output'])
        self.assertEqual(self.agent.history[-1], message('Hello, Alice!'))

    async def test_interrupted_stream_clears_history_without_retry(self):
        await self.agent.api.aclose()
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, content='data: {"type":"response.output_text.delta","delta":"Hi"}\n\n')

        self.agent.api = httpx.AsyncClient(base_url='https://api.openai.com/v1/',
                                         transport=httpx.MockTransport(respond))
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

    async def test_call_limit_stops_repeated_execution(self):
        self.outputs = [[call(self.aliases['greet'], {'name': 'Alice'}, f'call-{n}')] for n in range(3)]
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
        config['servers'][0]['config']['url'] = str(server.make_url('/'))
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
        Load the shared model profiles through the shipped client/client.jsonc.
        Returns:
            dict: The parsed models file.
        """
        return load_models(self.shipped_config(), CLIENT_CONFIG)

    def shipped_config(self):
        """
        Load the client config the package ships (client/client.jsonc).
        Returns:
            dict: The parsed config.
        """
        return json5.loads(CLIENT_CONFIG.read_text())

    async def test_model_profiles_come_from_the_shared_models_file(self):
        config = self.shipped_models()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key-not-real'}):
            for name in ('LOCAL_LLM_BASE_URL', 'LOCAL_LLM_MODEL', 'LOCAL_LLM_API_KEY'):
                os.environ.pop(name, None)
            with patch('mcpagent.client.agent.loaded_model', return_value=None):  # No loaded model reported
                local = resolve_model(config)  # "default": "local"
            self.assertEqual((local['base_url'], local['model']), ('http://boba:1234/v1', 'qwen/qwen3-coder-30b'))
            with patch('mcpagent.client.agent.loaded_model', return_value='qwen/loaded-now'):
                self.assertEqual(resolve_model(config)['model'], 'qwen/loaded-now')  # model_auto
                self.assertEqual(resolve_model(config, model='explicit')['model'], 'explicit')
            self.assertEqual(local['api_key'], 'lm-studio')  # the OpenAI key is never used for another server
            os.environ['LOCAL_LLM_MODEL'] = 'from-env'
            self.assertEqual(resolve_model(config, 'local')['model'], 'from-env')
            self.assertEqual(resolve_model(config, 'local', model='from-cli')['model'], 'from-cli')
            self.assertEqual(resolve_model(config, 'openai')['api_key'], 'test-key-not-real')

    async def test_instructions_come_from_the_shared_context_file(self):
        config_file = CLIENT_CONFIG
        instructions = load_instructions(self.shipped_config(), config_file)
        self.assertTrue(instructions.startswith('You are an agent'))
        self.assertIn('allowed folder', instructions)
        self.assertEqual(load_instructions({}, config_file), '')  # no instructions_file configured

    async def test_missing_key_and_unknown_profile(self):
        config = self.shipped_models()
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            with self.assertRaisesRegex(ValueError, 'OPENAI_API_KEY'):
                resolve_model(config, 'openai')
        with self.assertRaisesRegex(ValueError, "Unknown model profile 'nope'.*local, openai"):
            resolve_model(config, 'nope')
        with self.assertRaisesRegex(ValueError, "missing model"):
            resolve_model({'profiles': {'broken': {'base_url': 'http://x', 'api_key': 'k'}}}, 'broken')
        with self.assertRaisesRegex(ValueError, 'models_file'):
            load_models({}, CLIENT_CONFIG)

    async def test_local_profile_sends_its_own_key_to_its_own_server(self):
        seen = []

        def respond(request):
            seen.append((str(request.url), request.headers['Authorization']))
            return httpx.Response(200, json={"status": "completed", "output": [message('Hi')]})

        settings = resolve_model(self.shipped_models(), 'local')
        local = MCPAgent(self.agent.mcp, base_url=settings['base_url'], model=settings['model'],
                           api_key=settings['api_key'], provider=settings['name'],
                           api_transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(local.api.aclose)
        local.tools = self.agent.tools
        self.assertEqual(await local.ask('Hello'), 'Hi')
        self.assertEqual(seen, [('http://boba:1234/v1/responses', 'Bearer lm-studio')])


class OutputTests(unittest.TestCase):
    """The terminal layout shared by the three agents (README.md, "Terminal output")."""

    def test_layout_settings_come_from_the_shared_context_file(self):
        settings = load_output_settings(json5.loads(CLIENT_CONFIG.read_text()), CLIENT_CONFIG)
        self.assertEqual((settings['width'], settings['show_time']), (120, True))

    def test_token_counts_follow_the_response_time(self):
        printed = io.StringIO()
        output = Output(Console(file=printed), {'width': 120, 'show_time': True, 'show_tokens': True})
        output.add_usage(1200, 34)
        output.finish()
        self.assertRegex(printed.getvalue(), r'Response time: \d+\.\ds · tokens: 1,200 in, 34 out \(1 model call\)\n$')

    def test_lines_and_streamed_answer_wrap_and_the_response_is_timed(self):
        printed = io.StringIO()
        output = Output(Console(file=printed), {'width': 30, 'show_time': True})
        self.assertEqual(wrap('one two three four five six seven', 15), ['one two three', '  four five six', '  seven'])
        answer = 'The quiz service is running and pull request number one is still waiting for its quiz.'
        for i in range(0, len(answer), 7):
            output.text(answer[i:i + 7])
        output.finish()
        body = [line for line in printed.getvalue().split('\n') if line and not line.startswith('Response time')]
        self.assertTrue(all(len(line) <= 30 for line in body))
        self.assertEqual(' '.join(body), answer)
        self.assertRegex(printed.getvalue(), r'\n\nResponse time: \d+\.\ds\n$')


if __name__ == '__main__':
    unittest.main()
