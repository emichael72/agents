"""
Module: github.py

Description:
    `GitHub`: the gate's side of GitHub, through the `gh` CLI, which supplies the credentials:
    reading pull requests and their diffs, posting the `developer-quiz` commit status that branch
    protection on a gated repository requires, and keeping the gate's pull request comment. Each
    gated repository has its own `GitHub`; they share gh's sign-in.
"""

import json
import subprocess
from typing import Any, Optional


class GitHub:
    """
    One repository on GitHub, through the `gh` CLI.
    """

    STATUS_CONTEXT = "developer-quiz"  # The status check name branch protection requires
    TIMEOUT = 45

    def __init__(self, repo: str) -> None:
        """
        Args:
            repo: owner/name.
        """
        self.repo = repo
        self._info: Optional[dict[str, Any]] = None

    @classmethod
    def login(cls) -> str:
        """
        The account gh is signed in as.
        Returns:
            str: Its login.
        Raises:
            RuntimeError: If gh is not signed in or GitHub cannot be reached.
        """
        return json.loads(cls("").run("api", "user"))["login"]

    def info(self) -> dict[str, Any]:
        """The repository, as GitHub describes it to this account; read once."""
        if self._info is None:
            self._info = json.loads(self.run("api", f"repos/{self.repo}"))
        return self._info

    def default_branch(self) -> str:
        """The branch pull requests target, e.g. main."""
        return self.info()["default_branch"]

    def access_problems(self) -> list[str]:
        """
        What keeps the gate from gating this repository, and what lets merges past it.
        Returns:
            list[str]: "error: ..." when the gate cannot post its status (no access); "warning: ..."
                when the default branch does not require the status, so a merge need not wait for it.
        """
        try:
            info = self.info()
        except (RuntimeError, ValueError, KeyError) as exc:
            return [f"error: {self.repo} cannot be read with gh's sign-in: {exc}"]
        if not info.get("permissions", {}).get("push"):
            return [f"error: gh's account cannot post commit statuses to {self.repo} (it needs write access)."]
        base = info["default_branch"]
        try:
            branch = json.loads(self.run("api", f"repos/{self.repo}/branches/{base}"))
        except (RuntimeError, ValueError):
            return []  # Not knowing the protection is no reason to stop gating
        contexts = branch.get("protection", {}).get("required_status_checks", {}).get("contexts", [])
        if self.STATUS_CONTEXT not in contexts:
            return [f"warning: {self.repo}'s {base} does not require the {self.STATUS_CONTEXT} status, so merges "
                    f"need not wait for the gate (set it in the branch protection rules)."]
        return []

    def run(self, *args: str, payload: Optional[dict] = None) -> str:
        """
        Run the GitHub CLI.
        Args:
            *args: The `gh` arguments, e.g. ("api", "repos/owner/name/pulls/1").
            payload: JSON written to the command's stdin (for `--input -`).
        Returns:
            str: The command's standard output.
        Raises:
            RuntimeError: If the command fails or times out.
        """
        try:
            result = subprocess.run(
                ["gh", *args], input=json.dumps(payload) if payload is not None else None,
                capture_output=True, text=True, timeout=self.TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("GitHub request timed out") from exc
        if result.returncode:
            raise RuntimeError("GitHub request failed: " + result.stderr.strip()[:400])
        return result.stdout

    def pr_info(self, number: int) -> dict[str, Any]:
        """
        Read one pull request.
        Args:
            number: The PR number.
        Returns:
            dict: GitHub's pull request object.
        """
        return json.loads(self.run("api", f"repos/{self.repo}/pulls/{number}"))

    def open_prs(self) -> list[dict[str, Any]]:
        """
        List the open pull requests that target the default branch.
        Returns:
            list[dict]: GitHub's pull request objects.
        """
        base = self.default_branch()
        return json.loads(self.run("api", f"repos/{self.repo}/pulls?state=open&base={base}&per_page=100"))

    def pr_diff(self, number: int) -> str:
        """The pull request's unified diff."""
        return self.run("pr", "diff", str(number), "--repo", self.repo)

    def pr_commits(self, number: int) -> set[str]:
        """The SHAs of the pull request's commits."""
        return {c["sha"] for c in json.loads(self.run("api", f"repos/{self.repo}/pulls/{number}/commits?per_page=100"))}

    def publish_status(self, sha: str, state: str, description: str, target_url: str) -> None:
        """
        Post the `developer-quiz` commit status.
        Args:
            sha: The commit to mark.
            state: "pending", "success", "failure" or "error".
            description: The short text GitHub shows next to the check.
            target_url: Where the check's "Details" link points.
        """
        self.run("api", f"repos/{self.repo}/statuses/{sha}", "--method", "POST", "--input", "-", payload={
            "state": state, "context": self.STATUS_CONTEXT, "description": description[:140],
            "target_url": target_url})

    def comments(self, number: int) -> list[dict[str, Any]]:
        """The pull request's comments (its issue comments), oldest first."""
        return json.loads(self.run("api", f"repos/{self.repo}/issues/{number}/comments?per_page=100"))

    def add_comment(self, number: int, body: str) -> None:
        """Comment on a pull request."""
        self.run("api", f"repos/{self.repo}/issues/{number}/comments", "--method", "POST", "--input", "-",
                 payload={"body": body})

    def edit_comment(self, comment_id: int, body: str) -> None:
        """Replace a comment's text."""
        self.run("api", f"repos/{self.repo}/issues/comments/{comment_id}", "--method", "PATCH", "--input", "-",
                 payload={"body": body})
