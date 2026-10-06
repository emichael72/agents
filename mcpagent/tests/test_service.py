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
from unittest.mock import patch

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
            ('greet', {'name': 'Alice Smith'}, 'Hello, Alice Smith!'),
            ('greet', {}, f"Hello, {os.environ['USER']}!"),  # no name: the shell user
            ('rand', {'max': 1}, '1-1): 1'),
            ('wc', {'file': 'tools/greet/README.md'}, 'lines.'),
            ('cat', {'path': 'tools/greet/tool.json', 'count': 2}, 'lines 1-2 of'),
            ('sysinfo', {}, 'machine='),
            ('time', {'timezone': 'UTC'}, 'UTC (UTC+00:00)'),
            ('calc', {'expression': '(17 * 23) + sqrt(144)'}, '= 403'),
            ('ls', {'path': 'tools/greet'}, 'tool.json'),
            ('ls', {}, 'Allowed folders'),
            ('search_text', {'pattern': 'AGENT_NAME', 'path': 'tools/greet'}, 'tools/greet/greet.sh:'),
            ('df', {'path': 'tools/greet'}, 'tools/greet: '),
            ('git', {'path': 'tools', 'command': 'log', 'args': '-1 --date=short --format="%h %ad %s"'}, ' 20'),
        ]:
            result = (await self.rpc('tools/call', {'name': name, 'arguments': args}))['result']
            self.assertFalse(result['isError'], result)
            self.assertIn(expected, result['content'][0]['text'])

    @unittest.skipUnless(shutil.which('doxygen'), 'doxygen is not installed')
    async def test_doxy_reports_documentation_problems(self):
        good = '/** @file good.c\n * @brief Good. */\n\n/** @brief Add.\n * @param a A.\n * @param b B.\n' \
               ' * @return The sum. */\nint add(int a, int b) { return a + b; }\n'
        bad = '/** @file bad.c\n * @brief Bad. */\n\nint subtract(int a, int b) { return a - b; }\n'
        bare = 'int negate(int a) { return -a; }\n'
        with tempfile.TemporaryDirectory() as folder:
            for name, text in [('good.c', good), ('bad.c', bad), ('bare.h', bare)]:
                (Path(folder) / name).write_text(text)
            # Allow only the test folder, as "sample" (the tools read TOOLS_ALLOWED_PATHS instead)
            allowed = Path(folder) / 'allowed_paths.json'
            allowed.write_text(json.dumps({'paths': {'sample': folder}}))

            async def check(paths):
                with patch.dict(os.environ, {'TOOLS_ALLOWED_PATHS': str(allowed)}):
                    return (await self.rpc('tools/call', {'name': 'doxy', 'arguments': {'paths': paths}}))['result']

            result = await check('sample/good.c')
            self.assertFalse(result['isError'], result)
            self.assertIn('All documented: 1 file(s)', result['content'][0]['text'])
            text = (await check('sample/bad.c sample/bare.h'))['content'][0]['text']
            self.assertIn('Documentation problems: 2 in 2 file(s)', text)
            self.assertIn('sample/bad.c:4: error: Member subtract', text)
            self.assertIn('sample/bare.h:1: error: File has no @file', text)
            self.assertTrue((await check('sample/missing.c'))['isError'])
            self.assertTrue((await check(f'{folder}/good.c'))['isError'])  # Absolute paths are not allowed

    async def test_path_tools_stay_inside_the_allowed_folders(self):
        for path in ('tools/..', 'tools/greet/../..', 'etc', '/etc'):
            for name, args in [('ls', {'path': path}), ('cat', {'path': path + '/passwd'}),
                               ('wc', {'file': path + '/passwd'}), ('df', {'path': path}),
                               ('search_text', {'pattern': 'root', 'path': path}),
                               ('git', {'path': path, 'command': 'log'}), ('doxy', {'paths': path}),
                               ('make', {'path': path}), ('gcc', {'sources': path + '/x.c'})]:
                result = (await self.rpc('tools/call', {'name': name, 'arguments': args}))['result']
                self.assertTrue(result['isError'], (name, path))
        link = Path(__file__).resolve().parents[2] / 'tools' / 'greet' / 'escape-test-link'
        link.symlink_to('/etc')
        try:
            result = (await self.rpc('tools/call', {'name': 'ls',
                                                    'arguments': {'path': 'tools/greet/escape-test-link'}}))['result']
            self.assertTrue(result['isError'])
            self.assertIn('outside the allowed folder', result['content'][0]['text'])
        finally:
            link.unlink()

    async def test_make_gcc_and_git_work_inside_and_refuse_outside(self):
        source = '#include <stdio.h>\nint main(void) { int unused; printf("hi\\n"); return 0; }\n'
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'hello.c').write_text(source)
            (Path(folder) / 'Makefile').write_text('hello: hello.c\n\tcc -o hello hello.c\nclean:\n\trm -f hello\n')
            allowed = Path(folder) / 'allowed_paths.json'
            allowed.write_text(json.dumps({'paths': {'sample': folder}}))

            async def call(name, args):
                with patch.dict(os.environ, {'TOOLS_ALLOWED_PATHS': str(allowed)}):
                    result = (await self.rpc('tools/call', {'name': name, 'arguments': args}))['result']
                return result['isError'], result['content'][0]['text']

            error, text = await call('make', {'path': 'sample'})
            self.assertFalse(error, text)
            self.assertIn('make (default target) in sample: succeeded', text)
            self.assertTrue((Path(folder) / 'hello').exists())
            self.assertTrue((await call('make', {'path': 'sample', 'target': 'CC=evil'}))[0])
            self.assertTrue((await call('make', {'path': 'sample', 'target': '-f/etc/passwd'}))[0])

            error, text = await call('gcc', {'sources': 'sample/hello.c', 'flags': '-Wall'})
            self.assertFalse(error, text)
            self.assertIn('1 warning(s)', text)
            self.assertIn("sample/hello.c:2:", text)  # Paths shown as the model gives them
            error, text = await call('gcc', {'sources': 'sample/hello.c', 'output': 'sample/built'})
            self.assertFalse(error, text)
            self.assertTrue((Path(folder) / 'built').exists())
            for args in ({'sources': 'sample/hello.c', 'flags': '-fplugin=x.so'},
                         {'sources': 'sample/hello.c', 'output': '/tmp/built'},
                         {'sources': 'sample/hello.c', 'flags': '-I/etc'}):
                self.assertTrue((await call('gcc', args))[0], args)

        for args in ({'path': 'tools', 'command': 'commit', 'args': '-m x'},
                     {'path': 'tools', 'command': 'diff', 'args': '--no-index /etc/passwd x'},
                     {'path': 'tools', 'command': 'branch', 'args': 'new-branch'}):
            result = (await self.rpc('tools/call', {'name': 'git', 'arguments': args}))['result']
            self.assertTrue(result['isError'], args)

    async def test_ed_edits_inside_allowed_folders_and_protects_tools_and_git(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'a.c').write_text('int a;\nint b;\nint b;\n')
            (Path(folder) / '.git').mkdir()
            allowed = Path(folder) / 'allowed_paths.json'
            allowed.write_text(json.dumps({'paths': {'sample': folder, 'tools': str(Path(__file__).resolve().parents[2] / 'tools')}}))

            async def ed(args):
                with patch.dict(os.environ, {'TOOLS_ALLOWED_PATHS': str(allowed)}):
                    result = (await self.rpc('tools/call', {'name': 'ed', 'arguments': args}))['result']
                return result['isError'], result['content'][0]['text']

            error, text = await ed({'path': 'sample/a.c', 'old': 'int a;', 'new': 'int alpha;'})
            self.assertFalse(error, text)
            self.assertIn('1  int alpha;', text)
            error, text = await ed({'path': 'sample/a.c', 'old': 'int b;', 'new': 'int beta;'})
            self.assertTrue(error)
            self.assertIn('lines 2, 3', text)  # Ambiguous: where the matches are
            self.assertFalse((await ed({'path': 'sample/a.c', 'old': 'int b;', 'new': 'int beta;', 'all': True}))[0])
            self.assertFalse((await ed({'path': 'sample/a.c', 'action': 'lines', 'start': 3, 'new': ''}))[0])
            self.assertFalse((await ed({'path': 'sample/a.c', 'action': 'insert', 'line': 0, 'new': '/* top */'}))[0])
            self.assertFalse((await ed({'path': 'sample/b.h', 'action': 'write', 'new': '#pragma once'}))[0])
            self.assertEqual((Path(folder) / 'a.c').read_text(), '/* top */\nint alpha;\nint beta;\n')
            self.assertEqual((Path(folder) / 'b.h').read_text(), '#pragma once\n')
            for args in ({'path': 'tools/allowed_paths.json', 'action': 'write', 'new': '{}'},
                         {'path': 'sample/.git/config', 'action': 'write', 'new': 'x'},
                         {'path': 'sample/../escape.c', 'action': 'write', 'new': 'x'},
                         {'path': 'sample/a.c', 'old': 'missing', 'new': 'x'}):
                self.assertTrue((await ed(args))[0], args)
            self.assertFalse((Path(folder) / '.git' / 'config').exists())

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
            result = await self.rpc('tools/call', {'name': 'greet', 'arguments': arguments})
            self.assertEqual(result['error']['code'], -32602)
        for name, args in [('rand', {'max': 0}), ('wc', {'file': 'missing-file'})]:
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
