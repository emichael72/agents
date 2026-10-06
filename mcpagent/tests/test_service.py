"""
Module: test_service.py

Description:
    Tests for the MCP server (`MCPService`): tool discovery, real tool execution, argument
    validation and errors, the resource allowlist, and transport and origin checks.
"""
import sys
from pathlib import Path

# Run from any folder: the repository root holds the mcpagent package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import json
import os
import shutil
import tempfile
import unittest

import json5
from aiohttp.test_utils import TestClient, TestServer
from mcpagent import MCPService
from mcpagent.server.service import DEFAULT_CONFIG as SERVER_CONFIG


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    """Server tests: a real MCPService on the shared tools, called through an aiohttp test client."""
    async def asyncSetUp(self):
        """Start the server from server/server.jsonc on a test port and open a client to it."""
        config = SERVER_CONFIG
        old = Path.cwd()
        try:
            os.chdir(config.parent)
            self.service = MCPService(json5.loads(config.read_text()))
        finally:
            os.chdir(old)
        self.client = TestClient(TestServer(self.service._app))
        await self.client.start_server()

    async def asyncTearDown(self):
        """Close the test client and server."""
        await self.client.close()

    async def rpc(self, method, params=None):
        """
        Send one JSON-RPC request to the server and check that it answered with HTTP 200.
        Args:
            method: The JSON-RPC method, e.g. "tools/call".
            params: The method's parameters.
        Returns:
            dict: The JSON-RPC response.
        """
        response = await self.client.post('/', json={
            'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}
        })
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_discovery_and_all_tools(self):
        init = await self.rpc('initialize', {'protocolVersion': 'future'})
        self.assertEqual(init['result']['protocolVersion'], '2025-06-18')
        tools = await self.rpc('tools/list')
        shared_tools = Path(__file__).resolve().parents[2] / 'tools'  # agents/tools
        self.assertEqual(len(tools['result']['tools']), len(list(shared_tools.glob('*/tool.json'))))
        for name, args, expected in [
            ('greet_user', {'name': 'Alice Smith'}, 'Hello, Alice Smith!'),
            ('greet_user', {}, f"Hello, {os.environ['USER']}!"),  # no name: the shell user
            ('get_rand', {'max': 1}, '1-1): 1'),
            ('echo_message', {'message': 'hello world', 'repeat': 2}, 'HELLO WORLD'),
            ('count_lines', {'file': 'greet_user/README.md'}, 'lines.'),
            ('get_system_info', {}, 'machine='),
            ('current_time', {'timezone': 'UTC'}, 'UTC (UTC+00:00)'),
            ('calculate', {'expression': '(17 * 23) + sqrt(144)'}, '= 403'),
            ('list_files', {'path': 'greet_user'}, 'tool.json'),
            ('search_text', {'pattern': 'AGENT_NAME', 'path': 'greet_user'}, 'greet_user.sh:'),
            ('disk_usage', {'path': 'greet_user'}, 'greet_user: '),
            ('git_log', {'count': 1}, ' 20'),  # "<hash> <date> <subject>"
        ]:
            result = (await self.rpc('tools/call', {'name': name, 'arguments': args}))['result']
            self.assertFalse(result['isError'], result)
            self.assertIn(expected, result['content'][0]['text'])

    @unittest.skipUnless(shutil.which('doxygen'), 'doxygen is not installed')
    async def test_doxy_check_reports_documentation_problems(self):
        good = '/** @file good.c\n * @brief Good. */\n\n/** @brief Add.\n * @param a A.\n * @param b B.\n' \
               ' * @return The sum. */\nint add(int a, int b) { return a + b; }\n'
        bad = '/** @file bad.c\n * @brief Bad. */\n\nint subtract(int a, int b) { return a - b; }\n'
        bare = 'int negate(int a) { return -a; }\n'
        with tempfile.TemporaryDirectory() as folder:
            for name, text in [('good.c', good), ('bad.c', bad), ('bare.h', bare)]:
                (Path(folder) / name).write_text(text)

            async def check(paths):
                return (await self.rpc('tools/call', {'name': 'doxy_check', 'arguments': {'paths': paths}}))['result']

            result = await check(f'{folder}/good.c')
            self.assertFalse(result['isError'], result)
            self.assertIn('All documented: 1 file(s)', result['content'][0]['text'])
            text = (await check(f'{folder}/bad.c {folder}/bare.h'))['content'][0]['text']
            self.assertIn('Documentation problems: 2 in 2 file(s)', text)
            self.assertIn('bad.c:4: error: Member subtract', text)
            self.assertIn('bare.h:1: error: File has no @file', text)
            self.assertTrue((await check(f'{folder}/missing.c'))['isError'])

    def test_tool_manifests_are_discovered(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "hello").mkdir()
            (Path(folder) / "hello" / "tool.json").write_text(json.dumps({
                "description": "Say hello", "command": "echo", "args": ["hello"], "resource": "hello/README.md",
            }))
            tools = MCPService._discover_tools(folder, {"AGENT_NAME": "Test"})
        self.assertEqual(list(tools), ["hello"])
        self.assertEqual(tools["hello"]["working_dir"], folder)
        self.assertEqual(tools["hello"]["env"], {"AGENT_NAME": "Test"})
        self.assertEqual(tools["hello"]["resource"], str(Path(folder) / "hello" / "README.md"))

    async def test_errors(self):
        # A missing name is valid (greets the shell user); a wrong type, extra field or non-object is not.
        for arguments in ({'name': 123}, {'name': 'Alice', 'extra': True}, []):
            result = await self.rpc('tools/call', {'name': 'greet_user', 'arguments': arguments})
            self.assertEqual(result['error']['code'], -32602)
        for name, args in [('get_rand', {'max': 0}), ('count_lines', {'file': 'missing-file'})]:
            result = await self.rpc('tools/call', {'name': name, 'arguments': args})
            self.assertTrue(result['result']['isError'])

    async def test_resource_allowlist(self):
        resources = (await self.rpc('resources/list'))['result']['resources']
        result = await self.rpc('resources/read', {'uri': resources[0]['uri']})
        self.assertIn('contents', result['result'])
        result = await self.rpc('resources/read', {'uri': Path(__file__).resolve().as_uri()})
        self.assertEqual(result['error']['code'], -32602)

    async def test_transport_and_origin(self):
        response = await self.client.post('/', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertEqual(response.status, 202)
        self.assertEqual(await response.read(), b'')
        response = await self.client.get('/')
        self.assertEqual(response.status, 405)
        response = await self.client.post('/', json={}, headers={'Origin': 'https://untrusted.example'})
        self.assertEqual(response.status, 403)
        response = await self.client.get('/help')
        self.assertEqual(response.status, 200)
        response = await self.client.post('/', json={})
        self.assertEqual((await response.json())['error']['code'], -32600)


if __name__ == '__main__':
    unittest.main()
