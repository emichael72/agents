"""
Offline tests for the shell tool's commands.json: only installed commands are offered, and the
"environment" entry adds search folders and variables to the sandbox.
Run from the repository root:
    .venv/bin/python -m unittest discover -s tools/shell/tests
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from gatekeepers.fs.fs_gate import FsGate
from tools.shell.shell import Shell


class ShellConfigTests(unittest.TestCase):
    """A shell built from a temporary commands.json, over a writable "proj" and a read-only "docs"."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        for name in ("proj", "docs"):
            (self.folder / name / "bin").mkdir(parents=True)
            tool = self.folder / name / "bin" / f"{name}-tool"
            tool.write_text(f"#!/bin/sh\necho {name} tool ran\n")
            tool.chmod(0o755)
        self.paths = self.folder / "paths.json"
        self.paths.write_text(json.dumps({"paths": {
            "proj": {"path": str(self.folder / "proj"), "access": "rwx"},
            "docs": {"path": str(self.folder / "docs"), "access": "r"}}}))

    def shell(self, environment: Optional[dict[str, Any]] = None) -> Shell:
        """
        Build a shell from a commands.json holding a few commands and the given environment.
        Args:
            environment: commands.json's "environment"; None leaves it out.
        Returns:
            Shell: The shell.
        """
        settings: dict[str, Any] = {"commands": {
            "ls": "list folder contents", "cd": "change folder", "echo": "print text",
            "no-such-program-xyz": "not installed", "proj-tool": "a project script", "docs-tool": "a docs script"}}
        if environment is not None:
            settings["environment"] = environment
        commands = self.folder / "commands.json"
        commands.write_text(json.dumps(settings))
        with patch.object(Shell, "COMMANDS_FILE", commands):
            return Shell(FsGate.load(self.paths))

    def test_only_installed_commands_are_offered(self):
        shell = self.shell()
        self.assertEqual(sorted(shell.commands), ["cd", "echo", "ls"])  # cd is a bash builtin
        self.assertEqual(shell.missing, {"no-such-program-xyz", "proj-tool", "docs-tool"})
        self.assertNotIn("no-such-program-xyz", shell.help_text())

    def test_a_missing_command_is_refused_as_not_installed(self):
        shell = self.shell()
        with self.assertRaises(ValueError) as raised:
            shell.check("ls && no-such-program-xyz", self.folder / "proj")
        self.assertIn("not installed on this machine", str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            shell.check("python3 -c 1", self.folder / "proj")
        self.assertIn("not an allowed command", str(raised.exception))
        shell.check("cd . && ls", self.folder / "proj")  # Installed commands pass

    def test_search_folders_need_execute_access_and_must_exist(self):
        shell = self.shell({"path": ["/work/proj/bin", "/work/docs/bin", "/usr/no-such-folder", "/usr/bin"]})
        self.assertEqual(shell.search_path, [("/work/proj/bin", self.folder / "proj" / "bin"), ("/usr/bin", Path("/usr/bin"))])
        self.assertIn("proj-tool", shell.commands)
        self.assertIn("docs-tool", shell.missing)  # The docs folder has no execute access, so it is not searched
        for folder in ("relative/bin", "/etc", "/usr/../etc", "/tmp"):
            with self.subTest(folder=folder), self.assertRaisesRegex(ValueError, "under /usr or /work"):
                self.shell({"path": [folder]})

    def test_variables_are_expanded_and_the_sandbox_keeps_its_own(self):
        with patch.dict(os.environ, {"AGENT_NAME": "Test Agent"}):
            os.environ.pop("UNSET_XYZ", None)
            shell = self.shell({"variables": {"RUN_BY_AGENT": "1", "WHO": "${AGENT_NAME} via ${UNSET_XYZ}"}})
        self.assertEqual(shell.variables, {"RUN_BY_AGENT": "1", "WHO": "Test Agent via "})
        for variables in ({"PATH": "/tmp"}, {"HOME": "/x"}, {"GIT_CONFIG_VALUE_0": "hooks"}, {"LD_PRELOAD": "x.so"},
                          {"1BAD": "x"}, {"COUNT": 1}):
            with self.subTest(variables=variables), self.assertRaisesRegex(ValueError, "commands.json"):
                self.shell({"variables": variables})

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not installed")
    def test_the_sandbox_sees_the_search_folders_and_variables(self):
        with patch.dict(os.environ, {"AGENT_NAME": "Test Agent"}):
            shell = self.shell({"path": ["/work/proj/bin"], "variables": {"RUN_BY_AGENT": "1", "WHO": "${AGENT_NAME}"}})
        ok, text = shell.run("proj", 'echo "$RUN_BY_AGENT|$WHO|$PATH" && proj-tool')
        self.assertTrue(ok, text)
        self.assertIn("1|Test Agent|proj/bin:/usr/bin:/bin", text)  # Output shows /work/proj as proj
        self.assertIn("proj tool ran", text)

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not installed")
    def test_commands_get_no_input(self):
        shell = self.shell()
        shell.commands["cat"] = {"about": "print files"}
        shell.TIMEOUT = 5
        # An open pipe as this process's input, as the MCP server's protocol pipe is
        read_end, write_end = os.pipe()
        saved = os.dup(0)
        os.dup2(read_end, 0)
        try:
            ok, text = shell.run("proj", "cat")  # Reads its input: must end at once, not wait for the timeout
        finally:
            os.dup2(saved, 0)
            for fd in (saved, read_end, write_end):
                os.close(fd)
        self.assertTrue(ok, text)


if __name__ == "__main__":
    unittest.main()


class NetworkTests(unittest.TestCase):
    """Only curl and wget reach the network, and only when the caller allows it (the agents do, the gate does not)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        (self.folder / "proj").mkdir()
        self.paths = self.folder / "paths.json"
        self.paths.write_text(json.dumps({"paths": {"proj": {"path": str(self.folder / "proj"), "access": "rwx"}}}))
        self.commands = {"curl": {"about": "fetch a URL", "network": True}, "head": {"about": "first lines"},
                         "ls": {"about": "list"}}

    def sandbox_of(self, command: str, allow_network: bool) -> list[str]:
        """
        Run a command line with the sandbox stubbed out, and return the sandbox's arguments.
        Args:
            command: The command line.
            allow_network: What the caller allows (SHELL_NETWORK).
        Returns:
            list[str]: The bwrap command line it would have run.
        """
        shell = Shell(FsGate.load(self.paths), dict(self.commands), allow_network=allow_network)
        with patch("tools.shell.shell.subprocess.run") as run:
            run.return_value.stdout, run.return_value.returncode = "", 0
            shell.run("proj", command)
        return run.call_args.args[0]

    def test_a_network_command_gets_the_network_only_when_allowed(self):
        allowed = self.sandbox_of("curl -sS https://example.com | head -2", allow_network=True)
        self.assertEqual(allowed[1:3], ["--unshare-all", "--share-net"])  # Every other namespace stays private
        self.assertIn("/etc/resolv.conf", allowed)  # Name lookup works
        for command, allow in (("curl -sS https://example.com", False), ("ls | head -2", True)):
            with self.subTest(command=command, allow=allow):
                offline = self.sandbox_of(command, allow_network=allow)
                self.assertNotIn("--share-net", offline)
                self.assertNotIn("/etc/resolv.conf", offline)

    def test_the_shipped_settings(self):
        shipped = Shell.load_commands()
        self.assertEqual(sorted(name for name, entry in shipped.items() if entry.get("network")), ["curl", "wget"])
        manifest = json.loads((Path(__file__).resolve().parents[1] / "tool.json").read_text())
        self.assertEqual(manifest["env"], {"SHELL_NETWORK": "1"})  # The agents' shell tool allows it

    def test_the_pull_request_gate_builds_offline(self):
        from gatekeepers.pr.changes import ChangeInspector
        (self.folder / "proj" / "Makefile").write_text("all:\n\ttrue\n")
        with patch.dict(os.environ, {"SHELL_NETWORK": "1"}), patch("gatekeepers.pr.changes.subprocess.run") as run:
            run.return_value.stdout, run.return_value.stderr, run.return_value.returncode = "", "", 0
            ChangeInspector(lambda *_: "", "").check_build(self.folder / "proj")
        self.assertNotIn("SHELL_NETWORK", run.call_args.kwargs["env"])
