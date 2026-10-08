"""
Offline tests for the shell tool's command list: only installed commands are offered.
Run from the repository root:
    .venv/bin/python -m unittest discover -s tools/shell/tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from gatekeepers.fs.fs_gate import FsGate  # noqa: E402
from tools.shell.shell import Shell  # noqa: E402


class CommandListTests(unittest.TestCase):
    """A command in commands.json that is not installed is hidden from help and refused."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        commands = folder / "commands.json"
        commands.write_text(json.dumps({"commands": {
            "ls": "list folder contents", "cd": "change folder", "no-such-program-xyz": "not installed"}}))
        paths = folder / "paths.json"
        paths.write_text(json.dumps({"paths": {"work": str(folder)}}))
        with patch.object(Shell, "COMMANDS_FILE", commands):
            self.shell = Shell(FsGate.load(paths))
        self.folder = folder

    def test_only_installed_commands_are_offered(self):
        self.assertEqual(sorted(self.shell.commands), ["cd", "ls"])  # cd is a bash builtin
        self.assertEqual(self.shell.missing, {"no-such-program-xyz"})
        self.assertNotIn("no-such-program-xyz", self.shell.help_text())

    def test_a_missing_command_is_refused_as_not_installed(self):
        with self.assertRaises(ValueError) as raised:
            self.shell.check("ls && no-such-program-xyz", self.folder)
        self.assertIn("not installed on this machine", str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            self.shell.check("python3 -c 1", self.folder)
        self.assertIn("not an allowed command", str(raised.exception))
        self.shell.check("cd . && ls", self.folder)  # Installed commands pass


if __name__ == "__main__":
    unittest.main()
