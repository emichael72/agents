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
import pwd
import shutil
import subprocess
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
            ('sysinfo', {'section': 'software'}, 'python (running this tool)'),
            ('sysinfo', {}, '[cpu]'),
            ('time', {'timezone': 'UTC'}, 'UTC (UTC+00:00)'),
            ('time', {}, ':'),  # no time zone: local time
            ('shell', {'cwd': 'tools', 'command': 'ls time'}, 'tool.json'),
            ('shell', {'cwd': 'tools', 'command': 'help'}, 'Allowed folders'),
            ('shell', {'cwd': 'tools/time', 'command': 'grep -n timezone time.sh | head -1'}, 'timezone'),
            ('shell', {'cwd': 'tools', 'command': 'cat -n time/tool.json | wc -l'}, ''),
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
            # Allow only the test folder, as "sample" (the tools read FS_GATE_PATHS instead)
            allowed = Path(folder) / 'paths.json'
            allowed.write_text(json.dumps({'paths': {'sample': folder}}))

            async def check(paths):
                with patch.dict(os.environ, {'FS_GATE_PATHS': str(allowed)}):
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
        for path in ('tools/..', 'tools/time/../..', 'etc', '/etc'):
            for name, args in [('shell', {'cwd': path, 'command': 'ls'}), ('doxy', {'paths': path}),
                               ('ed', {'path': path + '/passwd', 'action': 'write', 'new': 'x'})]:
                result = (await self.rpc('tools/call', {'name': name, 'arguments': args}))['result']
                self.assertTrue(result['isError'], (name, path))
        link = Path(__file__).resolve().parents[2] / 'tools' / 'time' / 'escape-test-link'
        link.symlink_to('/etc')
        try:
            result = (await self.rpc('tools/call', {'name': 'shell', 'arguments': {
                'cwd': 'tools/time/escape-test-link', 'command': 'ls'}}))['result']
            self.assertTrue(result['isError'])
            self.assertIn('outside the allowed folder', result['content'][0]['text'])
        finally:
            link.unlink()

    @unittest.skipUnless(shutil.which('bwrap'), 'bubblewrap is not installed')
    async def test_shell_runs_allowed_commands_in_the_sandbox(self):
        source = '#include <stdio.h>\nint main(void) { int unused; printf("hi\\n"); return 0; }\n'
        tools = str(Path(__file__).resolve().parents[2] / 'tools')
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'rw').mkdir()
            (Path(folder) / 'rw' / 'hello.c').write_text(source)
            (Path(folder) / 'rw' / 'Makefile').write_text('hello: hello.c\n\tcc -Wall -o hello hello.c\n')
            (Path(folder) / 'rw' / 'locked').mkdir()
            (Path(folder) / 'ro').mkdir()
            (Path(folder) / 'ro' / 'Makefile').write_text('all:\n\ttrue\n')
            allowed = Path(folder) / 'paths.json'
            allowed.write_text(json.dumps({'paths': {
                'proj': {'path': str(Path(folder) / 'rw'), 'access': 'rwx', 'subpaths': {'locked': 'r'}},
                'docs': {'path': str(Path(folder) / 'ro'), 'access': 'r'},
                'tools': {'path': tools, 'access': 'r'}}}))

            async def shell(cwd, command):
                with patch.dict(os.environ, {'FS_GATE_PATHS': str(allowed)}):
                    result = (await self.rpc('tools/call', {'name': 'shell', 'arguments': {'cwd': cwd, 'command': command}}))['result']
                return result['isError'], result['content'][0]['text']

            proj = Path(folder) / 'rw'
            for args in (['init', '-q'], ['add', 'hello.c'], ['-c', 'user.name=T', '-c', 'user.email=t@x', 'commit', '-qm', 'first']):
                subprocess.run(['git', *args], cwd=proj, check=True, capture_output=True)
            error, text = await shell('proj', 'git log --format=%s && git status --short')
            self.assertFalse(error, text)
            self.assertIn('first', text)  # git reads
            for command in ('git commit -qam x', 'git checkout -b x', 'git branch new', 'git push', 'git -C .. log'):
                error, text = await shell('proj', command)  # but committing and branching belong to mr
                self.assertTrue(error, command)
                self.assertIn('mr tool', text)
            self.assertTrue((await shell('proj', 'touch .git/hooks/pre-commit'))[0])  # Hooks stay read-only
            if shutil.which('clang-format'):  # The agents' style template is found at /work/.clang-format
                error, text = await shell('proj', 'clang-format hello.c')
                self.assertFalse(error, text)
                self.assertEqual(json.loads(text)['logs'][1:3], ['int main(void)', '{'])
            error, text = await shell('proj', 'make && ./hello && grep -c include hello.c')
            self.assertFalse(error, text)
            self.assertIn('hi', text)
            self.assertIn('hello.c:2:', text.replace('proj/', ''))  # gcc's warning, paths as the model sees them
            # Refused by the check
            for cwd, command in (('proj', 'python3 -c 1'), ('proj', 'ls > x'), ('proj', 'ls $(echo /)'),
                                 ('proj', 'echo a#b; python3'), ('proj', 'cd .. && ls'), ('proj', 'make -C /work/docs'),
                                 ('docs', 'make'), ('docs', './x'), ('proj', 'ls\npython3')):
                self.assertTrue((await shell(cwd, command))[0], command)
            with patch.dict(os.environ, {'FS_GATE_PATHS': str(allowed)}):  # No cwd: the first allowed folder
                result = (await self.rpc('tools/call', {'name': 'shell', 'arguments': {'command': 'pwd'}}))['result']
            self.assertEqual(json.loads(result['content'][0]['text'])['logs'], ['proj'])
            # The sandbox knows only this user (a minimal /etc/passwd), and offers tar and shellcheck
            error, text = await shell('proj', 'whoami && cat /etc/passwd | wc -l && tar -czf /tmp/a.tgz hello.c && tar -tzf /tmp/a.tgz')
            self.assertFalse(error, text)
            self.assertEqual(json.loads(text)['logs'], [pwd.getpwuid(os.getuid()).pw_name, '1', 'hello.c'])
            if shutil.which('clang-tidy'):  # The agents' check template is found at /work/.clang-tidy
                error, text = await shell('proj', 'clang-tidy hello.c -- 2>&1 | grep -c warning: || true')
                self.assertFalse(error, text)
                self.assertNotIn('no checks enabled', text)
            for cwd, command in (('docs', 'cmake --version'), ('docs', 'gdb -batch ./x')):  # Need x access
                self.assertIn('needs x access', (await shell(cwd, command))[1])
            # Refused by the sandbox (the kernel)
            for cwd, command in (('docs', 'touch x'), ('proj', 'touch locked/x'), ('proj', 'cat /etc/os-release'),
                                 ('proj', 'ls /home'), ('proj', f'touch {tools}/x'), ('proj', 'touch /work/tools/x'),
                                 ('proj', 'git ls-remote https://github.com/x/y')):
                error, text = await shell(cwd, command)
                self.assertTrue(error, (command, text))
            self.assertFalse((Path(folder) / 'ro' / 'x').exists())
            self.assertFalse((Path(tools) / 'x').exists())

    async def test_mr_submits_changes_on_a_new_branch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote, repo, bin_dir = root / 'remote.git', root / 'repo', root / 'bin'
            bin_dir.mkdir()
            (bin_dir / 'gh').write_text(  # Stands in for GitHub: the PR, the repository and the gate's check
                '#!/bin/bash\ncase "$1 $2" in\n'
                '  "pr create") echo "$@" > "$(dirname "$0")/gh-args"; echo https://github.com/example/repo/pull/7 ;;\n'
                '  "repo view") echo example/repo ;;\n'
                '  api*) echo \'{"statuses": [{"context": "developer-quiz", "state": "pending", '
                '"description": "Complete the developer quiz", "target_url": "http://gate:8000/q/abc"}]}\' ;;\n'
                'esac\n')
            (bin_dir / 'gh').chmod(0o755)
            def run(*args, cwd=root):
                return subprocess.run(args, cwd=cwd, check=True, capture_output=True)
            run('git', 'init', '-q', '--bare', '-b', 'main', str(remote))
            run('git', 'clone', '-q', str(remote), str(repo))
            (repo / 'a.c').write_text('int a;\n')
            run('git', '-c', 'user.name=T', '-c', 'user.email=t@x', 'commit', '-q', '--allow-empty', '-m', 'start', cwd=repo)
            run('git', 'add', 'a.c', cwd=repo)
            run('git', '-c', 'user.name=T', '-c', 'user.email=t@x', 'commit', '-q', '-m', 'a', cwd=repo)
            run('git', 'push', '-q', 'origin', 'main', cwd=repo)
            run('git', 'remote', 'set-head', 'origin', 'main', cwd=repo)
            allowed = root / 'paths.json'
            allowed.write_text(json.dumps({'paths': {'proj': {'path': str(repo), 'access': 'rw'},
                                                     'look': {'path': str(repo), 'access': 'r'}}}))
            env = {'FS_GATE_PATHS': str(allowed), 'PATH': f"{bin_dir}:{os.environ['PATH']}",
                   'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@x', 'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@x'}

            async def mr(args):
                with patch.dict(os.environ, env):
                    result = (await self.rpc('tools/call', {'name': 'mr', 'arguments': args}))['result']
                return result['isError'], result['content'][0]['text']

            self.assertIn('no changes', (await mr({'path': 'proj', 'title': 'Nothing yet'}))[1])
            (repo / 'b.c').write_text('int  b( void ){return 1;}\n')  # Not in the template's style
            self.assertTrue((await mr({'path': 'look', 'title': 'Read-only folder'}))[0])
            error, text = await mr({'path': 'proj', 'title': 'Add b.c', 'body': 'A second file.'})
            self.assertFalse(error, text)
            self.assertIn('Opened https://github.com/example/repo/pull/7', text)
            self.assertIn('branch agent/add-b-c', text)
            self.assertIn('Quiz for the reviewer: http://gate:8000/q/abc', text)
            if shutil.which('clang-format'):  # The change was formatted before it was committed
                self.assertIn('Formatted with clang-format: b.c', text)
                committed = run('git', 'show', 'agent/add-b-c:b.c', cwd=repo).stdout.decode()
                self.assertEqual(committed, 'int b(void)\n{\n    return 1;\n}\n')
            self.assertIn('--base main --head agent/add-b-c --title Add b.c', (bin_dir / 'gh-args').read_text())
            heads = run('git', 'ls-remote', '--heads', str(remote)).stdout.decode()
            self.assertIn('refs/heads/agent/add-b-c', heads)
            branch = run('git', 'branch', '--show-current', cwd=repo).stdout.decode().strip()
            self.assertEqual(branch, 'main')  # Back on main, which did not move
            self.assertEqual(run('git', 'rev-list', '--count', 'main', cwd=repo).stdout.decode().strip(), '2')
            (repo / 'c.c').write_text('int c;\n')
            self.assertIn('already exists', (await mr({'path': 'proj', 'title': 'Add b.c'}))[1])

            # sync: a commit that reached GitHub elsewhere comes in; uncommitted changes block it
            self.assertIn('uncommitted changes', (await mr({'path': 'proj', 'action': 'sync'}))[1])
            (repo / 'c.c').unlink()
            other = root / 'other'
            run('git', 'clone', '-q', str(remote), str(other))
            (other / 'd.c').write_text('int d;\n')
            run('git', 'add', 'd.c', cwd=other)
            run('git', '-c', 'user.name=T', '-c', 'user.email=t@x', 'commit', '-q', '-m', 'Add d.c elsewhere', cwd=other)
            run('git', 'push', '-q', 'origin', 'main', cwd=other)
            error, text = await mr({'path': 'proj', 'action': 'sync'})
            self.assertFalse(error, text)
            self.assertIn('Add d.c elsewhere', text)
            self.assertTrue((repo / 'd.c').exists())
            self.assertIn('up to date', (await mr({'path': 'proj', 'action': 'sync'}))[1])

    async def test_memory_saves_reads_and_forgets_topics(self):
        with tempfile.TemporaryDirectory() as folder:
            allowed = Path(folder) / 'paths.json'
            (Path(folder) / 'memory').mkdir()
            allowed.write_text(json.dumps({'paths': {'memory': {'path': str(Path(folder) / 'memory'), 'access': 'rw'}}}))

            async def memory(args):
                with patch.dict(os.environ, {'FS_GATE_PATHS': str(allowed)}):
                    result = (await self.rpc('tools/call', {'name': 'memory', 'arguments': args}))['result']
                return result['isError'], json.loads(result['content'][0]['text'])['logs']

            self.assertEqual((await memory({'action': 'read'}))[1], ['The memory is empty.'])
            await memory({'action': 'save', 'topic': 'User Preferences', 'text': 'Prefers short answers'})
            await memory({'action': 'save', 'topic': 'user-preferences', 'text': 'Tests every new option'})
            error, logs = await memory({'action': 'read', 'topic': 'user-preferences'})
            self.assertFalse(error)
            self.assertEqual(logs, ['- Prefers short answers', '- Tests every new option'])
            index = (Path(folder) / 'memory' / 'index.md').read_text()
            self.assertIn('- **user-preferences**: Prefers short answers (updated ', index)
            self.assertTrue((await memory({'action': 'read', 'topic': 'nothing'}))[0])
            self.assertFalse((await memory({'action': 'forget', 'topic': 'user-preferences'}))[0])
            self.assertFalse((Path(folder) / 'memory' / 'user-preferences.md').exists())
            self.assertEqual((await memory({'action': 'save', 'topic': '../../etc', 'text': 'x'}))[0], False)
            self.assertTrue((Path(folder) / 'memory' / 'etc.md').exists())  # Made a plain name, stays inside

    async def test_ed_edits_inside_allowed_folders_and_protects_tools_and_git(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'a.c').write_text('int a;\nint b;\nint b;\n')
            (Path(folder) / '.git').mkdir()
            allowed = Path(folder) / 'paths.json'
            allowed.write_text(json.dumps({'paths': {'sample': {'path': folder, 'access': 'rw'},
                                                    'tools': str(Path(__file__).resolve().parents[2] / 'tools')}}))

            async def ed(args):
                with patch.dict(os.environ, {'FS_GATE_PATHS': str(allowed)}):
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
            for args in ({'path': 'context/paths.json', 'action': 'write', 'new': '{}'},
                         {'path': 'sample/.git/config', 'action': 'write', 'new': 'x'},
                         {'path': 'sample/../escape.c', 'action': 'write', 'new': 'x'},
                         {'path': 'sample/a.c', 'old': 'missing', 'new': 'x'}):
                self.assertTrue((await ed(args))[0], args)
            self.assertFalse((Path(folder) / '.git' / 'config').exists())

            # hex only reads: any file, binary too, in a read-only folder as well
            (Path(folder) / 'blob.bin').write_bytes(bytes(range(40)))
            error, text = await ed({'path': 'sample/blob.bin', 'action': 'hex', 'offset': 16, 'length': 18})
            self.assertFalse(error, text)
            self.assertEqual(json.loads(text)['logs'], [  # The server returns the output lines as logs
                'sample/blob.bin: bytes 16-33 (0x10-0x21) of 40',
                '00000010  10 11 12 13 14 15 16 17  18 19 1a 1b 1c 1d 1e 1f  |................|',
                '00000020  20 21                                             | !|',
                '(6 more bytes; continue with offset 34)'])
            error, text = await ed({'path': 'sample/blob.bin', 'action': 'hex', 'offset': -2})
            self.assertIn('00000026  26 27', text)
            self.assertNotIn('more bytes', text)
            error, text = await ed({'path': 'tools/ed/ed.py', 'action': 'hex', 'length': 16})
            self.assertFalse(error, text)
            self.assertIn('|#!/usr/bin/env p|', text)
            for args in ({'offset': 40}, {'length': 0}, {'length': 5000}):
                self.assertTrue((await ed({'path': 'sample/blob.bin', 'action': 'hex', **args}))[0], args)
            self.assertTrue((await ed({'path': 'sample/../escape.bin', 'action': 'hex'}))[0])

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
        # A missing time zone is valid (local time); a wrong type, extra field or non-object is not.
        for arguments in ({'timezone': 123}, {'timezone': 'UTC', 'extra': True}, []):
            result = await self.rpc('tools/call', {'name': 'time', 'arguments': arguments})
            self.assertEqual(result['error']['code'], -32602)
        for name, args in [('time', {'timezone': 'Not/AZone'}), ('shell', {'cwd': 'missing-file', 'command': 'ls'})]:
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
