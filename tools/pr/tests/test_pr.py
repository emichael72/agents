"""
Offline tests for the pr tool: temporary git repositories, with a local bare repository as GitHub,
and the gate's checks mocked where they would build. Run from the repository root:
    .venv/bin/python -m unittest discover -s tools/pr/tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pr import PullRequests  # noqa: E402

from gatekeepers.fs.fs_gate import FsGate  # noqa: E402
from gatekeepers.pr.changes import ChangeInspector  # noqa: E402


class PrToolTests(unittest.TestCase):
    """The check action, and open's refusal of a change that fails the gate's checks."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.remote, self.repo = root / "remote.git", root / "repo"
        self.git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=root)
        self.git("clone", "-q", str(self.remote), str(self.repo), cwd=root)
        for name in ("a.c", "b.c", "old.c"):
            (self.repo / name).write_text(f"/* {name} */\n")
        (self.repo / ".gitignore").write_text("/build/\n")
        self.git("add", "--all")
        self.git("commit", "-q", "-m", "start")
        self.git("push", "-q", "origin", "main")
        paths = root / "paths.json"
        paths.write_text(json.dumps({"paths": {"proj": {"path": str(self.repo), "access": "rwx"}}}))
        self.tool = PullRequests(gate=FsGate.load(paths))

    def git(self, *args, cwd=None):
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                       cwd=cwd or self.repo, check=True, capture_output=True)

    def change(self):
        """A modified file, a rename, a deletion, a new file with a space, and ignored build output."""
        (self.repo / "a.c").write_text("/* a.c, changed */\n")  # " M": the first porcelain line starts with a space
        self.git("mv", "old.c", "new.c")
        (self.repo / "b.c").unlink()
        (self.repo / "x y.h").write_text("/* new */\n")
        (self.repo / "build").mkdir()
        (self.repo / "build" / "stale.o").write_text("old build output")

    def test_changed_files_are_what_a_commit_would_hold(self):
        self.change()
        self.assertEqual(sorted(PullRequests.changed_files(self.repo)), ["a.c", "new.c", "x y.h"])

    def test_check_tree_sees_the_commit_not_the_ignored_files(self):
        self.change()
        seen = {}

        def build(_inspector, tree):
            seen["files"] = sorted(str(p.relative_to(tree)) for p in tree.rglob("*") if p.is_file())
            return True, "$ make && make check: succeeded\nlots of output"

        def docs(tree, changed):
            seen["changed"] = sorted(changed)
            return True, "All documented."

        with patch.object(ChangeInspector, "check_build", build), patch.object(ChangeInspector, "check_docs", staticmethod(docs)):
            ok, report = PullRequests.check_tree(self.repo)
        self.assertTrue(ok)
        self.assertEqual(seen["files"], [".gitignore", "a.c", "new.c", "x y.h"])
        self.assertEqual(seen["changed"], ["a.c", "new.c", "x y.h"])
        self.assertIn("Build and tests: passed\n$ make && make check: succeeded\n\nDocumentation: passed", report)
        self.assertNotIn("lots of output", report)  # A passing build shows only its summary line

    def test_a_binary_file_fails_the_check_before_any_build(self):
        self.change()
        (self.repo / "prog2").write_bytes(b"\x7fELF\0\0binary")  # Compiled by hand in the repository
        with patch.object(ChangeInspector, "check_build") as build:
            ok, report = PullRequests.check_tree(self.repo)
        self.assertFalse(ok)
        self.assertTrue(report.startswith("Binary files: prog2. A pull request holds source only"))
        build.assert_not_called()

    def test_check_reports_a_failure_as_an_error(self):
        failed = (False, "Build and tests: FAILED\nsrc/a.c:3:9: warning: unused variable")
        with patch.object(PullRequests, "check_tree", return_value=failed):
            with self.assertRaisesRegex(ValueError, "proj is not ready for a pull request(.|\n)*unused variable"):
                self.tool.check("proj")
        with patch.object(PullRequests, "check_tree", return_value=(True, "all passed")):
            self.assertIn("proj is ready for a pull request", self.tool.check("proj"))

    def test_open_refuses_a_change_that_fails_the_checks(self):
        self.change()
        failed = (False, "Documentation: FAILED\na.c:1: error: File has no @file documentation block")
        with patch.object(PullRequests, "check_tree", return_value=failed), \
                patch.object(PullRequests, "format_changes", return_value=[]):
            with self.assertRaisesRegex(ValueError, "Not opened(.|\n)*File has no @file"):
                self.tool.open_pr("proj", "Change things")
        branches = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=self.repo,
                                  capture_output=True, text=True).stdout.split()
        self.assertEqual(branches, ["main"])  # No branch, no commit
        self.assertEqual(sorted(PullRequests.changed_files(self.repo)), ["a.c", "new.c", "x y.h"])  # Work kept


if __name__ == "__main__":
    unittest.main()
