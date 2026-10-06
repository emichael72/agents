#!/usr/bin/env python3
"""
Module: mr.py

Description:
    Opens a merge request (a GitHub pull request) for the agents: the uncommitted changes of a
    repository in an allowed folder with write access go onto a new branch, which is pushed and
    proposed for merging into the repository's default branch.

    This is the one tool that reaches GitHub, with the credentials of the user running the agent
    (git and gh). It does one thing, the same way every time:
      1. Check the repository: on its default branch, no commits of its own, changes to submit.
      2. Bring the default branch up to date with GitHub (fast-forward only).
      3. Format the changed C/C++ files with clang-format (the repository's .clang-format, else
         the agents' template, context/clang-format.yaml), so every request follows the style.
      4. Create the branch (new, never the default branch), commit everything, push it.
      5. Open the pull request, then switch back to the default branch.
      6. Wait for the merge gate's check (MR_WAIT_CHECK, e.g. pr_gate's developer-quiz) on the new
         commit, up to MR_WAIT_SECONDS, and report it: for pr_gate, the quiz the reviewer must pass.
    It never pushes to the default branch, never force-pushes and never merges: merging stays with
    the people (and gates) of the repository.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# The file-system gate (context/paths.json) lives in agents/gatekeepers/fs
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gatekeepers" / "fs"))
import fs_gate  # noqa: E402

OPTIONS = ("title", "body", "branch")
CLANG_FORMAT = fs_gate.CONTEXT_DIR / "clang-format.yaml"  # The default style
FORMATTED = {".c", ".h", ".cc", ".cpp", ".hpp", ".cxx", ".hh"}
WAIT_CHECK = os.environ.get("MR_WAIT_CHECK", "")  # The status check to wait for; "" waits for none
WAIT_SECONDS = float(os.environ.get("MR_WAIT_SECONDS") or 0)
POLL_SECONDS = 3
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,80}$")
TIMEOUT = 60
# Hooks off: nothing in the repository runs while committing
GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


def git(repo: Path, *args: str, check: bool = True) -> str:
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
    result = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True, timeout=TIMEOUT)
    if check and result.returncode != 0:
        raise ValueError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()[:500]}")
    return result.stdout.strip()


def default_branch(repo: Path) -> str:
    """The branch the remote's HEAD points to (usually main)."""
    head = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    return head.removeprefix("origin/") or "main"


def format_changes(repo: Path) -> list[str]:
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
    for line in git(repo, "status", "--porcelain", "--untracked-files=all").splitlines():
        status, path = line[:2], line[3:].split(" -> ")[-1].strip('"')
        if "D" not in status and Path(path).suffix.lower() in FORMATTED and (repo / path).is_file():
            paths.append(path)
    own_style = any((repo / name).is_file() for name in (".clang-format", "_clang-format"))
    style = "file" if own_style else f"file:{CLANG_FORMAT}"
    changed = []
    for path in paths:
        before = (repo / path).read_bytes()
        subprocess.run(["clang-format", "-i", f"--style={style}", path], cwd=repo, capture_output=True, timeout=TIMEOUT)
        if (repo / path).read_bytes() != before:
            changed.append(path)
    return changed


def branch_name(title: str, branch: Optional[str]) -> str:
    """
    The branch to create: the one given, or agent/<title in lowercase words>.
    Args:
        title: The merge request's title.
        branch: The requested name, if any.
    Returns:
        str: A valid branch name.
    Raises:
        ValueError: If the requested name is not valid.
    """
    if branch:
        if not BRANCH.match(branch) or ".." in branch or branch.endswith((".lock", "/", ".")):
            raise ValueError(f"'{branch}' is not a valid branch name (letters, digits, . _ / -).")
        return branch
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].rstrip("-")
    return f"agent/{slug or 'change'}"


def wait_for_check(repo: Path, sha: str) -> str:
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
    if not WAIT_CHECK or WAIT_SECONDS <= 0:
        return ""
    name = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                          cwd=repo, capture_output=True, text=True, timeout=TIMEOUT).stdout.strip()
    deadline, status = time.monotonic() + WAIT_SECONDS, None
    while name and time.monotonic() < deadline:
        result = subprocess.run(["gh", "api", f"repos/{name}/commits/{sha}/status"], cwd=repo,
                                capture_output=True, text=True, timeout=TIMEOUT)
        if result.returncode == 0:
            statuses = [s for s in json.loads(result.stdout).get("statuses", []) if s.get("context") == WAIT_CHECK]
            status = statuses[0] if statuses else None  # Newest first
            if status and (status["state"] != "pending" or "/q/" in (status.get("target_url") or "")):
                break
        time.sleep(POLL_SECONDS)
    else:
        if status:
            return (f"Merge gate ({WAIT_CHECK}): still {status['state']} after {WAIT_SECONDS:g}s, "
                    f"{status.get('description', '')}: {status.get('target_url', '')}")
        return f"Merge gate ({WAIT_CHECK}): no result after {WAIT_SECONDS:g}s; see the pull request's checks."
    url = status.get("target_url") or ""
    if status["state"] == "pending":
        return f"Merge gate ({WAIT_CHECK}): pending, {status.get('description', '')}.\nQuiz for the reviewer: {url}"
    return f"Merge gate ({WAIT_CHECK}): {status['state']}, {status.get('description', '')}: {url}"


def open_mr(path: str, title: str, body: str = "", branch: Optional[str] = None) -> str:
    """
    Submit a repository's uncommitted changes as a merge request.
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
    folder, shown = fs_gate.resolve(path, "dir", "w")
    repo = Path(git(folder, "rev-parse", "--show-toplevel")).resolve()
    if not fs_gate.locate(repo, fs_gate.load_allowed()):
        raise ValueError(f"The repository of '{shown}' starts outside the allowed folders.")

    base = default_branch(repo)
    if git(repo, "branch", "--show-current") != base:
        raise ValueError(f"The repository is not on {base}. Make the changes on {base}, without "
                         f"committing; this tool creates the branch and the commit.")
    if not git(repo, "status", "--porcelain"):
        raise ValueError("There are no changes to submit.")
    git(repo, "fetch", "--quiet", "origin", base)
    if git(repo, "rev-list", "--count", f"origin/{base}..HEAD") != "0":
        raise ValueError(f"{base} has local commits that are not on GitHub; this tool only submits uncommitted changes.")
    git(repo, "merge", "--ff-only", "--quiet", f"origin/{base}")  # Up to date, as the merge gate requires

    name = branch_name(title, branch)
    if name == base or git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False) \
            or git(repo, "ls-remote", "--heads", "origin", name):
        raise ValueError(f"The branch '{name}' already exists; give another branch name.")

    agent = os.environ.get("AGENT_NAME")
    description = body.strip() + (f"\n\nOpened by the {agent}." if agent else "")
    formatted = format_changes(repo)
    git(repo, "switch", "--quiet", "-c", name)
    try:
        git(repo, "add", "--all")
        git(repo, "commit", "--quiet", "-m", title, *(["-m", body.strip()] if body.strip() else []))
        commit = git(repo, "rev-parse", "--short", "HEAD")
        sha = git(repo, "rev-parse", "HEAD")
        changed = git(repo, "show", "--stat", "--format=", "HEAD")
        git(repo, "push", "--quiet", "-u", "origin", name)
        result = subprocess.run(["gh", "pr", "create", "--base", base, "--head", name, "--title", title,
                                 "--body", description or title], cwd=repo, capture_output=True, text=True,
                                timeout=TIMEOUT)
        if result.returncode != 0:
            raise ValueError(f"The branch {name} was pushed, but the pull request failed: {result.stderr.strip()[:500]}")
        url = result.stdout.strip().splitlines()[-1]
    finally:
        git(repo, "switch", "--quiet", base, check=False)  # The changes now live on the branch
    gate = wait_for_check(repo, sha)
    note = f"\nFormatted with clang-format: {', '.join(formatted)}" if formatted else ""
    return f"Opened {url}\nbranch {name} (commit {commit}) into {base}:\n{changed}{note}" + (f"\n\n{gate}" if gate else "")


def main(argv: Optional[list[str]] = None) -> str:
    """
    Read "<path> --title <text> [--body <text>] [--branch <name>]" (values taken verbatim) and open
    the merge request.
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        str: The result.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    options: dict = {}
    while argv:
        argument = argv.pop(0)
        if argument.startswith("--") and argument[2:] in OPTIONS and argv:
            options[argument[2:]] = argv.pop(0)
        elif "path" not in options:
            options["path"] = argument
        else:
            raise ValueError(f"Unexpected argument '{argument}'.")
    if "path" not in options or "title" not in options:
        raise ValueError("Give the repository folder (e.g. core_dump) and a title.")
    return open_mr(options["path"], options["title"], options.get("body", ""), options.get("branch"))


if __name__ == "__main__":
    try:
        print(main())
    except (ValueError, OSError, subprocess.TimeoutExpired) as e:
        print(f"Error: {e}")
        sys.exit(1)
