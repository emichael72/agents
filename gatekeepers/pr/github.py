"""
Module: github.py

Description:
    `GitHub`: the gate's side of GitHub, through the `gh` CLI, which supplies the credentials:
    reading pull requests and their diffs, posting the `developer-quiz` commit status that branch
    protection on the gated repository requires, and keeping the gate's pull request comment.
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
        List the open pull requests that target main.
        Returns:
            list[dict]: GitHub's pull request objects.
        """
        return json.loads(self.run("api", f"repos/{self.repo}/pulls?state=open&base=main&per_page=100"))

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
