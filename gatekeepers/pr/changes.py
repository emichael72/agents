"""
Module: changes.py

Description:
    Inspects what a pull request changes, before any quiz is written: whether it builds and its
    tests pass (make, then make check, in the shell tool's sandbox), whether its C/C++ files are
    correctly documented (the doxy tool), and whether the change is only cosmetic (comments,
    formatting, documentation files) rather than code.

    `ChangeInspector` provides:
      - `inspect`: downloads the PR's head and merge-base trees from GitHub and returns the
        documentation result and the cosmetic verdict for the changed files.
      - `check_build`: runs the build and the tests (BUILD_COMMAND, then make TEST_TARGET when the
        Makefile has that target) on a whole tree, inside the shell tool's sandbox.
      - `check_docs`: runs doxy on a whole tree and keeps the problems in the changed files.
        The whole tree is checked so that a function documented in an unchanged header still
        counts as documented.
      - `code_tokens` / `is_cosmetic`: compares C/C++ sources with comments and formatting removed.

    Key design points:
      - The cosmetic verdict is mechanical, so a model cannot wave a code change through by calling
        it cosmetic. Only C/C++ sources (compared token by token) and documentation files count;
        any other changed file (a Makefile, a script) is treated as a code change.
      - Downloaded code is built and its tests run only inside the shell tool's sandbox (bubblewrap):
        the tree is the only folder it sees, with no network and nothing else of the host.
"""

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from gatekeepers import TOOLS_DIR

REPORT_LINES = 60
WARNING = re.compile(r"^\S+:\d+(:\d+)?: warning: ", re.M)  # gcc / clang: file:line[:column]: warning: ...

C_EXTENSIONS = {".c", ".h", ".cc", ".cpp", ".hpp", ".cxx", ".hh"}
DOC_EXTENSIONS = {".md", ".txt", ".rst", ".dox"}
DOC_NAMES = {"LICENSE", "AUTHORS", "CHANGELOG", "NOTICE"}

# One C/C++ token: a comment, a string or character literal, a word or number, whitespace, or one
# other character. Literals are matched whole, so comment markers inside them are not comments.
TOKEN = re.compile(r"""
    (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<literal>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')
  | (?P<word>\w+)
  | (?P<newline>\n)
  | (?P<space>[ \t\r\f\v]+|\\\n)
  | (?P<other>.)
""", re.VERBOSE | re.DOTALL)


class ChangeInspector:
    """
    Inspects the revisions of the gated repository's pull requests: build, documentation, and
    whether a change is only cosmetic.
    """

    DOXY = TOOLS_DIR / "doxy" / "doxy.sh"
    SHELL = TOOLS_DIR / "shell" / "shell.py"

    def __init__(self, gh: Callable[..., str], repo: str, build_command: str = "make", test_target: str = "check",
                 fail_on_warnings: bool = True) -> None:
        """
        Args:
            gh: The GitHub CLI runner: takes gh's arguments, returns its output.
            repo: owner/name.
            build_command: The build command line (QUIZ_BUILD_COMMAND); "" skips the build check.
            test_target: The make target that runs the tests (QUIZ_TEST_TARGET), run when the
                Makefile defines it.
            fail_on_warnings: Count compiler warnings as a build failure (QUIZ_FAIL_ON_WARNINGS).
        """
        self.gh = gh
        self.repo = repo
        self.build_command = build_command
        self.test_target = test_target
        self.fail_on_warnings = fail_on_warnings

    def inspect(self, number: int, head: str, base_sha: str) -> dict[str, Any]:
        """
        Inspect a PR revision: its documentation and whether it only changes comments and formatting.
        Args:
            number: The PR number.
            head: The head SHA to inspect.
            base_sha: The tip of the branch the PR targets; the change is measured from the merge base.
        Returns:
            dict: build_ok (bool), build_report (str), docs_ok (bool), docs_report (str), cosmetic
                (bool) and code_files (the changed files whose code changed, for the model's prompt).
        Raises:
            RuntimeError: If a GitHub request fails.
        """
        repo = self.repo
        files = json.loads(self.gh("api", f"repos/{repo}/pulls/{number}/files?per_page=100"))
        compare = json.loads(self.gh("api", f"repos/{repo}/compare/{base_sha}...{head}"))
        base = compare["merge_base_commit"]["sha"]

        with tempfile.TemporaryDirectory() as work:
            after_root = self.download_tree(repo, head, Path(work) / "head")
            before_root = self.download_tree(repo, base, Path(work) / "base")
            code_files = []
            for f in files:
                old_path = f.get("previous_filename", f["filename"])
                before = None if f["status"] == "added" else self.read(before_root, old_path)
                after = None if f["status"] == "removed" else self.read(after_root, f["filename"])
                if not self.is_cosmetic(f["filename"], before, after):
                    code_files.append(f["filename"])
            changed_c = [f["filename"] for f in files
                         if f["status"] != "removed" and Path(f["filename"]).suffix.lower() in C_EXTENSIONS]
            docs_ok, docs_report = self.check_docs(after_root, changed_c)
            build_ok, build_report = self.check_build(after_root)
        return {"build_ok": build_ok, "build_report": build_report, "docs_ok": docs_ok, "docs_report": docs_report,
                "cosmetic": not code_files, "code_files": code_files}

    @staticmethod
    def code_tokens(source: str) -> list[str]:
        """
        Reduce C/C++ source to the tokens that matter to the compiler: comments and formatting are
        dropped, while line ends are kept inside preprocessor directives (where they end the directive).
        Args:
            source: The file's text.
        Returns:
            list[str]: The tokens.
        """
        tokens: list[str] = []
        in_directive = False
        at_line_start = True
        for match in TOKEN.finditer(source):
            kind, text = match.lastgroup, match.group()
            if kind == "newline":
                if in_directive:
                    tokens.append("\n")
                in_directive, at_line_start = False, True
            elif kind in ("comment", "space"):
                continue
            else:
                if at_line_start and text == "#":
                    in_directive = True
                at_line_start = False
                tokens.append(text)
        return tokens

    @staticmethod
    def is_cosmetic(path: str, before: Optional[str], after: Optional[str]) -> bool:
        """
        Decide whether one file's change leaves the code as it was.
        Args:
            path: The file's path in the repository.
            before: Its text before the change; None if the PR adds it.
            after: Its text after the change; None if the PR removes it.
        Returns:
            bool: True for documentation files, and for C/C++ files whose code tokens did not change.
        """
        name = Path(path)
        if name.suffix.lower() in DOC_EXTENSIONS or name.name in DOC_NAMES:
            return True
        if name.suffix.lower() in C_EXTENSIONS:
            return ChangeInspector.code_tokens(before or "") == ChangeInspector.code_tokens(after or "")
        return False

    def check_build(self, tree: Path) -> tuple[bool, str]:
        """
        Build a tree and run its tests in the shell tool's sandbox, which sees only this tree.
        Args:
            tree: The checked-out repository.
        Returns:
            tuple[bool, str]: Whether the build (and tests) succeeded, and the report: what ran and the
                end of its output.
        """
        makefile = next((tree / name for name in ("GNUmakefile", "makefile", "Makefile") if (tree / name).is_file()), None)
        build_command, test_target = self.build_command, self.test_target
        if not build_command or makefile is None:
            return True, "No Makefile: nothing to build."
        has_tests = bool(test_target) and re.search(rf"^{re.escape(test_target)}\s*:", makefile.read_text(errors="replace"), re.M)
        command = build_command + (f" && make {test_target}" if has_tests else "")
        allowed = Path(tempfile.mkdtemp()) / "paths.json"
        allowed.write_text(json.dumps({"paths": {"pr": {"path": str(Path(tree).resolve()), "access": "rwx"}}}))
        try:
            result = subprocess.run(["python3", str(self.SHELL), "--cwd", "pr", "--command", command], capture_output=True,
                                    text=True, timeout=120, env={**os.environ, "FS_GATE_PATHS": str(allowed)})
        except subprocess.TimeoutExpired:
            return False, f"{command}: timed out."
        finally:
            shutil.rmtree(allowed.parent, ignore_errors=True)
        output = (result.stdout or result.stderr).rstrip().removeprefix("Error: ")
        warnings = [line for line in output.splitlines() if WARNING.match(line)]
        lines = output.splitlines()
        if len(lines) > REPORT_LINES:
            lines = [f"... {len(lines) - REPORT_LINES} earlier lines"] + lines[-REPORT_LINES:]
        note = "" if has_tests else f" (no '{test_target}' target in the Makefile, so no tests ran)"
        ok = result.returncode == 0 and not (self.fail_on_warnings and warnings)
        if result.returncode != 0:
            outcome = "FAILED"
        elif warnings:
            outcome = f"built with {len(warnings)} compiler warning(s)" + (", which fail the check" if self.fail_on_warnings else "")
        else:
            outcome = "succeeded"
        # The warnings first: a long build's output is cut, and they may be in the part left out
        return ok, "\n".join([f"$ {command}{note}: {outcome}", *warnings, *([""] if warnings else []), *lines])

    @staticmethod
    def check_docs(tree: Path, changed: list[str]) -> tuple[bool, str]:
        """
        Run doxy on a whole tree and keep the problems reported for the changed files.
        Args:
            tree: The checked-out repository.
            changed: The changed C/C++ files, relative to the tree.
        Returns:
            tuple[bool, str]: Whether the changed files are correctly documented, and the report: the
                problems (file:line: message), a short confirmation, or why the check could not run.
        """
        if not changed:
            return True, "No C/C++ files changed."
        # doxy only reads folders that allowed-paths file names: allow just this tree, as "pr"
        allowed = Path(tempfile.mkdtemp()) / "paths.json"
        allowed.write_text(json.dumps({"paths": {"pr": str(Path(tree).resolve())}}))
        try:
            result = subprocess.run(["bash", str(ChangeInspector.DOXY), "pr"], cwd=tree, capture_output=True, text=True,
                                    timeout=120, env={**os.environ, "FS_GATE_PATHS": str(allowed)})
        except subprocess.TimeoutExpired:
            return False, "The documentation check timed out."
        finally:
            shutil.rmtree(allowed.parent, ignore_errors=True)
        if result.returncode:
            return False, "The documentation check could not run: " + (result.stdout or result.stderr).strip()

        # Group each problem with its continuation lines, then keep the changed files' problems
        problems: list[list[str]] = []
        for line in result.stdout.splitlines()[1:]:
            if line.startswith((" ", "\t")) and problems:
                problems[-1].append(line)
            elif re.match(r"^[^ ].*:\d+: ", line):
                problems.append([line])
        wanted = set(changed)
        problems = [[p[0].removeprefix("pr/"), *p[1:]] for p in problems]  # Paths as in the repository
        kept = ["\n".join(p) for p in problems if p[0].split(":", 1)[0] in wanted]
        if not kept:
            return True, f"All {len(changed)} changed C/C++ file(s) are documented."
        return False, "\n".join(kept)

    @staticmethod
    def download_tree(repo: str, sha: str, dest: Path) -> Path:
        """
        Download and unpack one commit's files from GitHub.
        Args:
            repo: owner/name.
            sha: The commit.
            dest: An empty folder to unpack into.
        Returns:
            Path: The unpacked repository's root folder.
        Raises:
            RuntimeError: If the download fails.
        """
        result = subprocess.run(["gh", "api", f"repos/{repo}/tarball/{sha}"], capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError("GitHub download failed: " + result.stderr.decode(errors="replace").strip()[:400])
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            archive.extractall(dest, filter="data")  # Refuses absolute paths, links out of dest, devices
        (root,) = [p for p in dest.iterdir() if p.is_dir()]  # GitHub wraps the files in one folder
        return root

    @staticmethod
    def read(root: Path, path: Optional[str]) -> Optional[str]:
        """Read a file of a downloaded tree as text, or None if it does not exist there."""
        if path is None or not (root / path).is_file():
            return None
        return (root / path).read_text(encoding="utf-8", errors="replace")
