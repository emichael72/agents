"""
Module: pr_gate.py

Description:
    Command line for the developer quiz, and the entry point the agents run as the `pr_gate` tool
    (python -m gatekeepers.pr.pr_gate, through pr_gate.sh).

    Commands:
      - status [--project NAME] [--pr N] [--action status|history|start]: Each gated project's open
        PRs and their quiz state, with links (the agents' tool); past assessments; or start the
        service first. The agents cannot stop or restart the gate: that is the user's, with
        ./install.sh --gate stop|restart.
      - create PR [--project NAME] [--profile NAME] [--fixed FILE]: Create (or re-post) the quiz
        for a PR now.
      - list: Every stored quiz, as JSON.
      - serve [--host --port --poll --profile]: Run the web service and the GitHub poller.

    A project is a folder marked "pr_gated" in context/paths.json, named by its folder name
    (core_dump) or its repository (owner/name); --project may be left out when only one is gated.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from typing import Optional

import httpx

from gatekeepers import __version__
from gatekeepers.pr.gate import QuizGate


class PrGateCli:
    """
    The pr_gate commands, over one gate.
    """

    UNIT = "pr-gate"  # The gate's systemd user unit; the only service the agents may control
    SERVICE_ACTIONS = ("start",)  # Never stop or restart: the agents must not turn off their own gate

    def __init__(self, gate: QuizGate) -> None:
        """
        Args:
            gate: The gate the commands report on and drive.
        """
        self.gate = gate

    @staticmethod
    def running(port: int = 8000) -> bool:
        """Whether the service answers its health check."""
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=3).raise_for_status()
            return True
        except httpx.HTTPError:
            return False

    def control(self, action: str, port: int = 8000) -> str:
        """
        Start the gate's own systemd user unit (UNIT), and nothing else. The agents may bring the gate
        up, never take it down: stopping and restarting are left to ./install.sh --gate.
        Args:
            action: "start".
            port: The service port, to wait until it answers after a start.
        Returns:
            str: What was done, and the resulting service state.
        Raises:
            ValueError: If the action is unknown, the unit is not installed, or systemctl fails.
        """
        unit = self.UNIT
        if action not in self.SERVICE_ACTIONS:
            raise ValueError(f"Unknown action '{action}'; use status, history or start. Only the user stops "
                             "or restarts the gate (./install.sh --gate stop|restart).")
        if subprocess.run(["systemctl", "--user", "cat", unit], capture_output=True).returncode != 0:
            raise ValueError(f"The {unit} service is not installed; run ./install.sh --gate install.")
        result = subprocess.run(["systemctl", "--user", action, unit], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise ValueError(f"systemctl {action} {unit} failed: {(result.stderr or result.stdout).strip()[:300]}")
        who = os.environ.get("AGENT_NAME", "the command line")
        logging.getLogger("pr_gate").info("%s %s by %s", unit, action, who)
        for _ in range(20):  # Wait until it answers, up to 10 s
            if self.running(port):
                return (f"Started the {unit} service (asked by {who}); "
                        f"it is running at {self.gate.settings.base_url}.")
            time.sleep(0.5)
        return f"Started the {unit} service, but it does not answer yet; see ./install.sh --gate logs."

    def history(self, project: Optional[str] = None, pr: Optional[int] = None, limit: int = 30) -> str:
        """
        Describe past assessments, newest first, as the History page shows them.
        Args:
            project: Only this project (folder name or owner/name); None for all.
            pr: Only this pull request, of the one project; needs project when several are gated.
            limit: At most this many assessments.
        Returns:
            str: One line per assessment: when (UTC), repository, pull request and title, revision,
                outcome, attempts and best score; then the History page's link.
        """
        self.gate.init_store()
        repo = self.gate.project(project).repo if project or pr else None
        rows = self.gate.history(repo, pr)
        if not rows:
            return "No assessments yet" + (f" for {repo} PR #{pr}." if pr else f" for {repo}." if repo else ".")
        lines = [f"{len(rows)} assessment(s){f' of PR #{pr}' if pr else ''}, newest first (times in UTC):"]
        for row in rows[:limit]:
            attempts = (f"{row['attempts']} attempt(s), best {row['best']}/{row['total']}" if row["attempts"]
                        else "no attempts")
            reported = f", {row['reported']} question(s) reported as wrong" if row.get("reported") else ""
            lines.append(f"{row['created'][:16]}  {row['repo']} PR #{row['pr']} {row['pr_title']}  {row['sha'][:7]}  "
                         f"{row['outcome']}  ({attempts}{reported})")
        if len(rows) > limit:
            lines.append(f"... {len(rows) - limit} older assessment(s)")
        query = (f"?repo={repo}" + (f"&pr={pr}" if pr else "")) if repo else ""
        lines.append(f"History page: {self.gate.settings.base_url}/history{query}")
        return "\n".join(lines)

    def status(self, project: Optional[str] = None, pr: Optional[int] = None, port: int = 8000) -> str:
        """
        Describe each gated project's open PRs and their quizzes.
        Args:
            project: Only this project (folder name or owner/name); None for all.
            pr: Only this PR, of the one project; needs project when several are gated.
            port: The local service port, for the health check.
        Returns:
            str: One line about the service, then per project a line naming it and one line per PR.
        """
        gate, settings = self.gate, self.gate.settings
        gate.init_store()
        if self.running(port):
            lines = [f"pr_gate service: running at {settings.base_url}"]
        else:
            lines = ["pr_gate service: not running, so new commits are not assessed (start it with action start)"]
        projects = [gate.project(project)] if project or pr else list(gate.projects.values())
        if not projects:
            lines.append("No gated projects: set \"pr_gated\": true on a folder in context/paths.json.")
        lines += [f"Not gated: {line}" for line in settings.skipped]
        for gated in projects:
            lines += self.project_status(gated.repo, gated.name, pr)
        if any("quiz waiting" in line for line in lines):
            lines.append("A waiting quiz is for the pull request's author, a person, to take: checking again will not "
                         "change it. Give the user the quiz link and stop.")
        return "\n".join(lines)

    def project_status(self, repo: str, name: str, pr: Optional[int] = None) -> list[str]:
        """
        Describe one project's open PRs and their quizzes.
        Args:
            repo: The repository, owner/name.
            name: The project's folder name.
            pr: Only this PR; None for every open PR that targets the default branch.
        Returns:
            list[str]: A line naming the project, then one line per PR, or why GitHub could not tell.
        """
        gate, github = self.gate, self.gate.github(repo)
        lines = [f"{name} ({repo}):"]
        try:
            prs = [github.pr_info(pr)] if pr else github.open_prs()
        except RuntimeError as exc:  # One repository's trouble must not hide the others
            return lines + [f"  Could not read its pull requests: {exc}"]
        if not prs:
            lines.append(f"  No open pull requests target {github.default_branch()}.")
        for info in prs:
            head = info["head"]["sha"]
            title = f"  PR #{info['number']} '{info['title']}' by {info['user']['login']} at {head[:7]}"
            if info["state"] != "open":
                state = f"{info['state']}, not assessed"
            elif info["user"]["login"] != gate.developer:
                state = f"not assessed (only {gate.developer}'s PRs are)"
            else:
                row = gate.find_quiz(repo, info["number"], head, info["base"]["sha"])
                if row is None:
                    state = "no assessment yet; the service makes one within a minute"
                elif not row["build_ok"]:
                    state = f"build or tests failed, merge blocked: {gate.quiz_url(row['id'])}"
                elif not row["docs_ok"]:
                    state = f"documentation problems, merge blocked: {gate.quiz_url(row['id'])}"
                elif row["cosmetic"]:
                    state = "cosmetic change (comments/formatting only), documentation OK, may merge"
                elif row["passed"] and row.get("skipped"):
                    state = "quiz skipped (proof-of-concept mode), may merge"
                elif row["passed"]:
                    state = "quiz passed, may merge"
                else:
                    state = f"quiz waiting, merge blocked: {gate.quiz_url(row['id'])}"
            lines.append(f"{title}: {state}")
        return lines

    def create(self, pr: int, project: Optional[str], profile: Optional[str], fixed: Optional[str]) -> str:
        """
        Assess a PR's current revision now, or re-post its status.
        Args:
            pr: The PR number.
            project: Its project (folder name or owner/name); None when only one is gated.
            profile: The model profile; None uses the default.
            fixed: A JSON quiz file to use instead of the model.
        Returns:
            str: The quiz's id, repository, PR, revision, source and link, as JSON.
        """
        row = self.gate.create(self.gate.project(project).repo, pr, profile, fixed)
        return json.dumps({"id": row["id"], "repo": row["repo"], "pr": row["pr"], "sha": row["sha"],
                           "source": row["source"], "url": self.gate.quiz_url(row["id"])}, indent=2)

    def quizzes(self) -> str:
        """Every stored quiz, as JSON."""
        self.gate.init_store()
        return json.dumps(self.gate.store.quizzes(), indent=2)

    def serve(self, host: str, port: int, poll: float, profile: Optional[str]) -> None:
        """
        Run the web service, with the GitHub poller unless poll is 0.
        Args:
            host: Bind address.
            port: Port.
            poll: Seconds between GitHub polls; 0 disables polling.
            profile: The model profile for generation; None uses the default.
        """
        import uvicorn
        from gatekeepers.pr.server import GateApp, Poller

        gate, settings = self.gate, self.gate.settings
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] %(message)s")
        log = logging.getLogger("pr_gate")
        model = gate.generator.resolve_model(profile or settings.profile)  # Fail now, not at the first push
        for problem in gate.access_problems():  # Drops the projects gh's sign-in cannot gate
            log.warning("%s", problem)
        projects = ", ".join(f"{p.name} ({p.repo})" for p in gate.projects.values()) or "no projects"
        log.info("Quizzes for %s by %s, model %s / %s, links at %s", projects, gate.developer, model["name"],
                 model["model"], settings.base_url)
        app = GateApp(gate, Poller(gate, poll, profile) if poll > 0 else None).app
        uvicorn.run(app, host=host, port=port, access_log=False)

    def run(self, args: argparse.Namespace) -> int:
        """
        Run one parsed command.
        Args:
            args: The command line, from `build_parser`.
        Returns:
            int: 0.
        """
        if args.command == "status":
            if args.action == "history":
                print(self.history(args.project, args.pr))
                return 0
            if args.action != "status":
                print(self.control(args.action, args.port))
            print(self.status(args.project, args.pr, args.port))
        elif args.command == "create":
            print(self.create(args.pr, args.project, args.profile, args.fixed))
        elif args.command == "list":
            print(self.quizzes())
        else:
            self.serve(args.host, args.port, args.poll, args.profile)
        return 0

    @classmethod
    def build_parser(cls, poll_seconds: float) -> argparse.ArgumentParser:
        """
        Build the command-line parser.
        Args:
            poll_seconds: The default seconds between GitHub polls (QUIZ_POLL_SECONDS).
        Returns:
            argparse.ArgumentParser: The parser.
        """
        parser = argparse.ArgumentParser(description="Developer quiz: PRs merge only after their author passes a quiz")
        parser.add_argument("-v", "--version", action="version", version=f"pr_gate {__version__}")
        sub = parser.add_subparsers(dest="command", required=True)

        project_help = "The gated project: its folder name in context/paths.json or owner/name; " \
                       "optional when only one is gated"
        status_cmd = sub.add_parser("status", help="Show the open PRs and their quiz state")
        status_cmd.add_argument("--project", help=project_help + " (default: all)")
        status_cmd.add_argument("--pr", type=int, help="Only this PR (of --project)")
        status_cmd.add_argument("--port", type=int, default=8000, help="Local service port (default 8000)")
        status_cmd.add_argument("--action", default="status", choices=("status", "history", *cls.SERVICE_ACTIONS),
                                help="status (default), history of past assessments, or start the pr-gate "
                                     "service first")

        create_cmd = sub.add_parser("create", help="Create (or re-post) the quiz for a PR's current revision")
        create_cmd.add_argument("pr", type=int)
        create_cmd.add_argument("--project", help=project_help)
        create_cmd.add_argument("--profile", help="Model profile from context/models.json (local or openai)")
        create_cmd.add_argument("--fixed", help="Use a JSON quiz file instead of the model")

        sub.add_parser("list", help="List every stored quiz as JSON")

        serve_cmd = sub.add_parser("serve", help="Run the web service and the GitHub poller")
        serve_cmd.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
        serve_cmd.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
        serve_cmd.add_argument("--poll", type=float, default=poll_seconds,
                               help=f"Seconds between GitHub polls; 0 disables (default {poll_seconds:g}, "
                                    "QUIZ_POLL_SECONDS)")
        serve_cmd.add_argument("--profile", help="Model profile from context/models.json (local or openai)")
        return parser


def main(argv: Optional[list[str]] = None) -> int:
    """
    Load the gate, parse the command line and run the command.
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        int: 0 on success, 1 on an error (printed as "Error: ..."), 2 on a usage error.
    """
    try:
        gate = QuizGate.load()
        args = PrGateCli.build_parser(gate.settings.poll_seconds).parse_args(argv)
        return PrGateCli(gate).run(args)
    except (ValueError, RuntimeError, httpx.HTTPError) as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
