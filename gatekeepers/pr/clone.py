"""
Module: clone.py

Description:
    Keeps a local clone of the gated repository current with GitHub, so agents always start from
    the latest code: their shell has no network, and a model may forget the pr tool's sync action.

    The service calls `sync_clone` every few seconds (QUIZ_SYNC_SECONDS). It touches the clone
    only when that is safe, and then only fast-forwards:
      - The clone is on its default branch, with no changes at all (a new or edited file means
        someone is working there), and no commits of its own.
      - Anything else is left alone and reported, never merged, reset or overwritten.
    git hooks are off while it runs, so nothing in the repository runs.
"""

import subprocess
from pathlib import Path

GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
TIMEOUT = 60


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run git in a repository, without raising."""
    return subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True, timeout=TIMEOUT)


def sync_clone(path: str) -> tuple[str, str]:
    """
    Fast-forward a clone's default branch to GitHub's, when that is safe.
    Args:
        path: The clone's folder (~ allowed).
    Returns:
        tuple[str, str]: The outcome, "updated", "current" or "skipped", and a sentence about it.
    """
    repo = Path(path).expanduser()
    if not (repo / ".git").exists():
        return "skipped", f"{repo} is not a git clone."
    head = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD").stdout.strip()
    base = head.removeprefix("origin/") or "main"
    if _git(repo, "branch", "--show-current").stdout.strip() != base:
        return "skipped", f"{repo} is not on {base}."
    if _git(repo, "status", "--porcelain").stdout.strip():
        return "skipped", f"{repo} has changes in progress."
    fetched = _git(repo, "fetch", "--quiet", "origin", base)
    if fetched.returncode != 0:
        return "skipped", f"Could not fetch {base}: {fetched.stderr.strip()[:200]}"
    if _git(repo, "rev-list", "--count", f"origin/{base}..HEAD").stdout.strip() != "0":
        return "skipped", f"{repo}'s {base} has commits that are not on GitHub."
    incoming = _git(repo, "log", "--oneline", f"HEAD..origin/{base}").stdout.strip()
    if not incoming:
        return "current", f"{repo} is up to date."
    merged = _git(repo, "merge", "--ff-only", "--quiet", f"origin/{base}")
    if merged.returncode != 0:
        return "skipped", f"Could not fast-forward {repo}: {merged.stderr.strip()[:200]}"
    count = len(incoming.splitlines())
    return "updated", f"{repo}: {base} fast-forwarded by {count} commit(s), now {incoming.splitlines()[0]}"

