"""
Module: pr.py

Description:
    Opens a GitHub pull request for the agents (users may call it a merge request, MR): the
    uncommitted changes of a repository in an allowed folder with write access go onto a new
    branch, which is pushed and proposed for merging into the repository's default branch.

    This is the one tool that reaches GitHub, with the credentials of the user running the agent
    (git and gh). With action sync, it brings the default branch up to date with GitHub
    (fast-forward only), which the agents' sandboxed shell cannot do. The open action creates a pull
    request, the same way every time:
      1. Check the repository: on its default branch, no commits of its own, changes to submit.
      2. Bring the default branch up to date with GitHub (fast-forward only).
      3. Format the changed C/C++ files with clang-format (the repository's .clang-format, else
         the agents' template, context/clang-format.yaml), so every pull request follows the style.
      4. Create the branch (new, never the default branch), commit everything, push it.
      5. Open the pull request, then switch back to the default branch.
      6. Wait for the merge gate's check (PR_WAIT_CHECK, e.g. pr_gate's developer-quiz) on the new
         commit, up to PR_WAIT_SECONDS, and report it: for pr_gate, the quiz the reviewer must pass.
    It never pushes to the default branch, never force-pushes and never merges: merging stays with
    the people (and gates) of the repository.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Import the shared filesystem gate from the repository root (the nearest folder above
# holding pyproject.toml).
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents
                            if (p / "pyproject.toml").is_file())))
from gatekeepers import CONTEXT_DIR
from gatekeepers.fs.fs_gate import FsGate
from tools.common.cli import ToolArgumentParser


class PullRequests:
    """
    Opens pull requests from, and syncs, the repositories in the allowed folders.
    """

    VERSION = "1.0.0"
    CLANG_FORMAT = CONTEXT_DIR / "clang-format.yaml"  # The default style
    FORMATTED = {".c", ".h", ".cc", ".cpp", ".hpp", ".cxx", ".hh"}
    POLL_SECONDS = 3
    BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,80}$")
    TIMEOUT = 60
    # Hooks off: nothing in the repository runs while committing
    GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]

    def __init__(self, gate: Optional[FsGate] = None, wait_check: str = "", wait_seconds: float = 0) -> None:
        """
        Args:
            gate: The allowed folders; None reads context/paths.json.
            wait_check: The status check to wait for after opening (PR_WAIT_CHECK); "" waits for none.
            wait_seconds: How long to wait for it (PR_WAIT_SECONDS).
        """
        self.gate = gate or FsGate.load()
        self.wait_check = wait_check
        self.wait_seconds = wait_seconds

    @classmethod
    def git(cls, repo: Path, *args: str, check: bool = True) -> str:
        """
        Run git in a repository.
        Args:
            repo: The repository.
            *args: git's arguments.
            check: Raise if git fails.
        Returns:
            str: Its output, stripped.
        Raises:
            ValueError: If git fails and check is set.
        """
        result = subprocess.run([*cls.GIT, "-C", str(repo), *args], capture_output=True, text=True, timeout=cls.TIMEOUT)
        if check and result.returncode != 0:
            raise ValueError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()[:500]}")
        return result.stdout.strip()

    @classmethod
    def default_branch(cls, repo: Path) -> str:
        """The branch the remote's HEAD points to (usually main)."""
        head = cls.git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
        return head.removeprefix("origin/") or "main"

    @classmethod
    def format_changes(cls, repo: Path) -> list[str]:
        """
        Format the changed and new C/C++ files with clang-format, in place.
        The style is the repository's own .clang-format when it has one, else the agents' template.
        Args:
            repo: The repository.
        Returns:
            list[str]: The files clang-format changed (empty when clang-format is not installed).
        """
        if not shutil.which("clang-format"):
            return []
        paths = []
        for line in cls.git(repo, "status", "--porcelain", "--untracked-files=all").splitlines():
            status, path = line[:2], line[3:].split(" -> ")[-1].strip('"')
            if "D" not in status and Path(path).suffix.lower() in cls.FORMATTED and (repo / path).is_file():
                paths.append(path)
        own_style = any((repo / name).is_file() for name in (".clang-format", "_clang-format"))
        style = "file" if own_style else f"file:{cls.CLANG_FORMAT}"
        changed = []
        for path in paths:
            before = (repo / path).read_bytes()
            subprocess.run(["clang-format", "-i", f"--style={style}", path], cwd=repo, capture_output=True,
                           timeout=cls.TIMEOUT)
            if (repo / path).read_bytes() != before:
                changed.append(path)
        return changed

    @classmethod
    def branch_name(cls, title: str, branch: Optional[str]) -> str:
        """
        The branch to create: the one given, or agent/<title in lowercase words>.
        Args:
            title: The pull request's title.
            branch: The requested name, if any.
        Returns:
            str: A valid branch name.
        Raises:
            ValueError: If the requested name is not valid.
        """
        if branch:
            if not cls.BRANCH.match(branch) or ".." in branch or branch.endswith((".lock", "/", ".")):
                raise ValueError(f"'{branch}' is not a valid branch name (letters, digits, . _ / -).")
            return branch
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].rstrip("-")
        return f"agent/{slug or 'change'}"

    def wait_for_check(self, repo: Path, sha: str) -> str:
        """
        Wait for the merge gate's status check on a commit to be ready, and describe it. With pr_gate,
        the check is pending with "Checking documentation..." while it works, then links to the quiz
        (a /q/ page) or settles to success or failure.
        Args:
            repo: The repository.
            sha: The pushed commit.
        Returns:
            str: The check's state, description and link, or why it is not known yet.
        """
        check, seconds = self.wait_check, self.wait_seconds
        if not check or seconds <= 0:
            return ""
        name = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                              cwd=repo, capture_output=True, text=True, timeout=self.TIMEOUT).stdout.strip()
        deadline = time.monotonic() + seconds
        status: Optional[dict[str, Optional[str]]] = None
        while name and time.monotonic() < deadline:
            result = subprocess.run(["gh", "api", f"repos/{name}/commits/{sha}/status"], cwd=repo,
                                    capture_output=True, text=True, timeout=self.TIMEOUT)
            if result.returncode == 0:
                statuses = [s for s in json.loads(result.stdout).get("statuses", []) if s.get("context") == check]
                status = statuses[0] if statuses else None  # Newest first
                if status and (status["state"] != "pending" or "/q/" in (status.get("target_url") or "")):
                    break
            time.sleep(self.POLL_SECONDS)
        else:
            if status:
                return (f"Merge gate ({check}): still {status['state']} after {seconds:g}s, "
                        f"{status.get('description', '')}: {status.get('target_url', '')}")
            return f"Merge gate ({check}): no result after {seconds:g}s; see the pull request's checks."
        url = status.get("target_url") or ""
        if status["state"] == "pending":
            return f"Merge gate ({check}): pending, {status.get('description', '')}.\nQuiz for the reviewer: {url}"
        return f"Merge gate ({check}): {status['state']}, {status.get('description', '')}: {url}"

    def sync(self, path: str) -> str:
        """
        Bring a repository's default branch up to date with GitHub (fast-forward only), so work starts
        from the latest code. The agents' shell has no network, so this is how they update.
        Args:
            path: <allowed name>/<folder> in the repository; it needs write access.
        Returns:
            str: What changed: the commits that came in, or that it was already up to date.
        Raises:
            ValueError: If the repository is not on its default branch, has uncommitted changes or
                commits of its own, or GitHub cannot be reached.
        """
        folder, shown = self.gate.resolve(path, "dir", "w")
        repo = Path(self.git(folder, "rev-parse", "--show-toplevel")).resolve()
        base = self.default_branch(repo)
        if self.git(repo, "branch", "--show-current") != base:
            raise ValueError(f"The repository is not on {base}; switch to it before syncing.")
        if self.git(repo, "status", "--porcelain"):
            raise ValueError("The repository has uncommitted changes; open a pull request with them, or undo them "
                             "(git restore in the shell), before syncing.")
        self.git(repo, "fetch", "--quiet", "origin", base)
        if self.git(repo, "rev-list", "--count", f"origin/{base}..HEAD") != "0":
            raise ValueError(f"{base} has commits that are not on GitHub; they need a person to sort out.")
        before = self.git(repo, "rev-parse", "--short", "HEAD")
        incoming = self.git(repo, "log", "--oneline", f"HEAD..origin/{base}")
        if not incoming:
            return f"{shown}: {base} is up to date with GitHub ({before})."
        self.git(repo, "merge", "--ff-only", "--quiet", f"origin/{base}")
        after = self.git(repo, "rev-parse", "--short", "HEAD")
        return f"{shown}: updated {base} from {before} to {after}:\n{incoming}"

    def open_pr(self, path: str, title: str, body: str = "", branch: Optional[str] = None) -> str:
        """
        Submit a repository's uncommitted changes as a pull request.
        Args:
            path: <allowed name>/<folder> in the repository; it needs write access.
            title: The title, also the commit message's first line.
            body: The description, also the rest of the commit message.
            branch: The branch to create; None derives it from the title.
        Returns:
            str: The pull request's URL, the branch, the commit and the files changed.
        Raises:
            ValueError: If the repository is not ready, the branch exists, or a step fails.
        """
        title = title.strip()
        if not 3 <= len(title) <= 120:
            raise ValueError("Give a title of 3 to 120 characters: what the change does.")
        folder, shown = self.gate.resolve(path, "dir", "w")
        repo = Path(self.git(folder, "rev-parse", "--show-toplevel")).resolve()
        if not self.gate.locate(repo):
            raise ValueError(f"The repository of '{shown}' starts outside the allowed folders.")

        base = self.default_branch(repo)
        if self.git(repo, "branch", "--show-current") != base:
            raise ValueError(f"The repository is not on {base}. Make the changes on {base}, without "
                             f"committing; this tool creates the branch and the commit.")
        if not self.git(repo, "status", "--porcelain"):
            raise ValueError("There are no changes to submit.")
        self.git(repo, "fetch", "--quiet", "origin", base)
        if self.git(repo, "rev-list", "--count", f"origin/{base}..HEAD") != "0":
            raise ValueError(f"{base} has local commits that are not on GitHub; this tool only submits uncommitted changes.")
        self.git(repo, "merge", "--ff-only", "--quiet", f"origin/{base}")  # Up to date, as the merge gate requires

        name = self.branch_name(title, branch)
        if name == base or self.git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False) \
                or self.git(repo, "ls-remote", "--heads", "origin", name):
            raise ValueError(f"The branch '{name}' already exists; give another branch name.")

        agent = os.environ.get("AGENT_NAME")
        description = body.strip() + (f"\n\nOpened by the {agent}." if agent else "")
        formatted = self.format_changes(repo)
        self.git(repo, "switch", "--quiet", "-c", name)
        try:
            self.git(repo, "add", "--all")
            self.git(repo, "commit", "--quiet", "-m", title, *(["-m", body.strip()] if body.strip() else []))
            commit = self.git(repo, "rev-parse", "--short", "HEAD")
            sha = self.git(repo, "rev-parse", "HEAD")
            changed = self.git(repo, "show", "--stat", "--format=", "HEAD")
            self.git(repo, "push", "--quiet", "-u", "origin", name)
            result = subprocess.run(["gh", "pr", "create", "--base", base, "--head", name, "--title", title,
                                     "--body", description or title], cwd=repo, capture_output=True, text=True,
                                    timeout=self.TIMEOUT)
            if result.returncode != 0:
                raise ValueError(f"The branch {name} was pushed, but the pull request failed: {result.stderr.strip()[:500]}")
            url = result.stdout.strip().splitlines()[-1]
        finally:
            self.git(repo, "switch", "--quiet", base, check=False)  # The changes now live on the branch
        check = self.wait_for_check(repo, sha)
        note = f"\nFormatted with clang-format: {', '.join(formatted)}" if formatted else ""
        return f"Opened {url}\nbranch {name} (commit {commit}) into {base}:\n{changed}{note}" + (f"\n\n{check}" if check else "")

    @classmethod
    def build_parser(cls) -> ToolArgumentParser:
        """
        The command line: "[--action=open|sync] [--title=...] [--body=...] [--branch=...] -- <path>".
        Returns:
            ToolArgumentParser: The parser.
        """
        parser = ToolArgumentParser("pr", cls.VERSION, "Open a pull request from a repository's changes, or sync it.")
        parser.add_argument("path", nargs="?", help="<allowed name>/<folder> in the repository, e.g. core_dump")
        parser.add_argument("--action", default="open", help="open (default): open a pull request; sync: update "
                                                             "the default branch from GitHub")
        parser.add_argument("--title", help="open: the title, also the commit message's first line")
        parser.add_argument("--body", default="", help="open: the description, also the rest of the commit message")
        parser.add_argument("--branch", help="open: the branch to create; omit it to derive one from the title")
        return parser

    def run(self, args: argparse.Namespace) -> str:
        """
        Run the action a command line asks for: open a pull request (the default), or sync.
        Args:
            args: The command line, from `build_parser`.
        Returns:
            str: The result.
        Raises:
            ValueError: If the arguments are incomplete, or a step fails.
        """
        if not args.path:
            raise ValueError("Give the repository folder, e.g. core_dump.")
        if args.action == "sync":
            return self.sync(args.path)
        if args.action != "open":
            raise ValueError(f"Unknown action '{args.action}'; use open (the default) or sync.")
        if args.title is None:
            raise ValueError("Give a title for the pull request.")
        return self.open_pr(args.path, args.title, args.body, args.branch)


def main(argv: Optional[list[str]] = None) -> int:
    """
    Parse the command line, run the action and print its result, or "Error: <reason>".
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        int: 0 on success, 1 on an error.
    """
    try:
        args = PullRequests.build_parser().parse_args(argv)
        tool = PullRequests(wait_check=os.environ.get("PR_WAIT_CHECK", ""),
                            wait_seconds=float(os.environ.get("PR_WAIT_SECONDS") or 0))
        print(tool.run(args))
    except (ValueError, OSError, subprocess.TimeoutExpired) as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
