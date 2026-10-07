#!/usr/bin/env python3
"""
Module: pr_gate.py

Description:
    Command line for the developer quiz, and the entry point the agents run as the `pr_gate` tool.

    Commands:
      - status [--pr N]: The open PRs and their quiz state, with links (the agents' tool).
      - create PR [--profile NAME] [--fixed FILE]: Create (or re-post) the quiz for a PR now.
      - list: Every stored quiz, as JSON.
      - serve [--host --port --poll --profile]: Run the web service and the GitHub poller.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from typing import Optional

# Third-party
import httpx

# Local imports
import quiz


UNIT = "pr-gate"  # The gate's systemd user unit; the only service the agents may control
SERVICE_ACTIONS = ("start", "stop", "restart")


def running(port: int = 8000) -> bool:
    """Whether the service answers its health check."""
    try:
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=3).raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def control(action: str, port: int = 8000) -> str:
    """
    Start, stop or restart the gate's own systemd user unit (UNIT), and nothing else. Stopping it
    cannot let a change through: the repository's branch protection still requires the gate's
    check, so pull requests simply wait until it runs again.
    Args:
        action: "start", "stop" or "restart".
        port: The service port, to wait until it answers after a start.
    Returns:
        str: What was done, and the resulting service state.
    Raises:
        ValueError: If the action is unknown, the unit is not installed, or systemctl fails.
    """
    if action not in SERVICE_ACTIONS:
        raise ValueError(f"Unknown action '{action}'; use status, {', '.join(SERVICE_ACTIONS)}.")
    if subprocess.run(["systemctl", "--user", "cat", UNIT], capture_output=True).returncode != 0:
        raise ValueError(f"The {UNIT} service is not installed; run ./install.sh --gate install.")
    result = subprocess.run(["systemctl", "--user", action, UNIT], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise ValueError(f"systemctl {action} {UNIT} failed: {(result.stderr or result.stdout).strip()[:300]}")
    who = os.environ.get("AGENT_NAME", "the command line")
    logging.getLogger("pr_gate").info("%s %s by %s", UNIT, action, who)
    if action == "stop":
        return f"Stopped the {UNIT} service (asked by {who}). Pull requests wait for its check until it runs again."
    for _ in range(20):  # Wait until it answers, up to 10 s
        if running(port):
            return f"{action.capitalize()}ed the {UNIT} service (asked by {who}); it is running at {quiz.BASE_URL}."
        time.sleep(0.5)
    return f"{action.capitalize()}ed the {UNIT} service, but it does not answer yet; see ./install.sh --gate logs."


def history(pr: Optional[int] = None, limit: int = 30) -> str:
    """
    Describe past assessments, newest first, as the History page shows them.
    Args:
        pr: Only this pull request; None for all.
        limit: At most this many assessments.
    Returns:
        str: One line per assessment: when (UTC), pull request and title, revision, outcome,
            attempts and best score; then the History page's link.
    """
    quiz.init()
    rows = quiz.history(pr)
    if not rows:
        return "No assessments yet" + (f" for PR #{pr}." if pr else ".")
    lines = [f"{len(rows)} assessment(s){f' of PR #{pr}' if pr else ''}, newest first (times in UTC):"]
    for row in rows[:limit]:
        attempts = (f"{row['attempts']} attempt(s), best {row['best']}/{row['total']}" if row["attempts"]
                    else "no attempts")
        lines.append(f"{row['created'][:16]}  PR #{row['pr']} {row['pr_title']}  {row['sha'][:7]}  "
                     f"{row['outcome']}  ({attempts})")
    if len(rows) > limit:
        lines.append(f"... {len(rows) - limit} older assessment(s)")
    lines.append(f"History page: {quiz.BASE_URL}/history" + (f"?pr={pr}" if pr else ""))
    return "\n".join(lines)


def status(pr: Optional[int] = None, port: int = 8000) -> str:
    """
    Describe the open PRs and their quizzes.
    Args:
        pr: Only this PR; None for every open PR that targets main.
        port: The local service port, for the health check.
    Returns:
        str: One line about the service, then one line per PR.
    """
    quiz.init()
    if running(port):
        lines = [f"pr_gate service: running at {quiz.BASE_URL}"]
    else:
        lines = ["pr_gate service: not running, so new commits are not assessed (start it with action start)"]

    prs = [quiz.pr_info(pr)] if pr else quiz.open_prs()
    if not prs:
        lines.append(f"No open pull requests target main in {quiz.REPO}.")
    for info in prs:
        head = info["head"]["sha"]
        title = f"PR #{info['number']} '{info['title']}' by {info['user']['login']} at {head[:7]}"
        if info["state"] != "open":
            state = f"{info['state']}, not assessed"
        elif info["user"]["login"] != quiz.DEVELOPER:
            state = f"not assessed (only {quiz.DEVELOPER}'s PRs are)"
        else:
            row = quiz.find_quiz(info["number"], head, info["base"]["sha"])
            if row is None:
                state = "no assessment yet; the service makes one within a minute"
            elif not row["build_ok"]:
                state = f"build or tests failed, merge blocked: {quiz.BASE_URL}/q/{row['id']}"
            elif not row["docs_ok"]:
                state = f"documentation problems, merge blocked: {quiz.BASE_URL}/q/{row['id']}"
            elif row["cosmetic"]:
                state = "cosmetic change (comments/formatting only), documentation OK, may merge"
            elif row["passed"] and row.get("skipped"):
                state = "quiz skipped (proof-of-concept mode), may merge"
            elif row["passed"]:
                state = "quiz passed, may merge"
            else:
                state = f"quiz waiting, merge blocked: {quiz.BASE_URL}/q/{row['id']}"
        lines.append(f"{title}: {state}")
    return "\n".join(lines)


def serve(host: str, port: int, poll: float, profile: Optional[str]) -> None:
    """
    Run the web service, with the GitHub poller unless poll is 0.
    Args:
        host: Bind address.
        port: Port.
        poll: Seconds between GitHub polls; 0 disables polling.
        profile: The model profile for generation; None uses the default.
    """
    import uvicorn
    from server import Poller, create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] %(message)s")
    model = quiz.resolve_model(profile or quiz.PROFILE)  # Fail now, not at the first push
    logging.getLogger("pr_gate").info("Quizzes for %s by %s, model %s / %s, links at %s",
                                      quiz.REPO, quiz.DEVELOPER, model["name"], model["model"], quiz.BASE_URL)
    app = create_app(Poller(poll, profile) if poll > 0 else None)
    uvicorn.run(app, host=host, port=port, access_log=False)


def main(argv: Optional[list[str]] = None) -> None:
    """
    Parse the command line and run the command.
    Args:
        argv: Arguments; None reads sys.argv.
    """
    parser = argparse.ArgumentParser(description="Developer quiz: PRs merge only after their author passes a quiz")
    sub = parser.add_subparsers(dest="command", required=True)

    status_cmd = sub.add_parser("status", help="Show the open PRs and their quiz state")
    status_cmd.add_argument("--pr", type=int, help="Only this PR")
    status_cmd.add_argument("--port", type=int, default=8000, help="Local service port (default 8000)")
    status_cmd.add_argument("--action", default="status", choices=("status", "history", *SERVICE_ACTIONS),
                            help="status (default), history of past assessments, or start, stop or restart "
                                 "the pr-gate service first")

    create_cmd = sub.add_parser("create", help="Create (or re-post) the quiz for a PR's current revision")
    create_cmd.add_argument("pr", type=int)
    create_cmd.add_argument("--profile", help="Model profile from context/models.json (local or openai)")
    create_cmd.add_argument("--fixed", help="Use a JSON quiz file instead of the model")

    sub.add_parser("list", help="List every stored quiz as JSON")

    serve_cmd = sub.add_parser("serve", help="Run the web service and the GitHub poller")
    serve_cmd.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
    serve_cmd.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
    serve_cmd.add_argument("--poll", type=float, default=quiz.POLL_SECONDS,
                           help=f"Seconds between GitHub polls; 0 disables (default {quiz.POLL_SECONDS:g}, QUIZ_POLL_SECONDS)")
    serve_cmd.add_argument("--profile", help="Model profile from context/models.json (local or openai)")

    args = parser.parse_args(argv)
    if args.command == "status":
        if args.action == "history":
            print(history(args.pr))
            return
        if args.action != "status":
            print(control(args.action, args.port))
            if args.action == "stop":
                return
        print(status(args.pr, args.port))
    elif args.command == "create":
        row = quiz.create_quiz(args.pr, args.profile, args.fixed)
        print(json.dumps({"id": row["id"], "pr": row["pr"], "sha": row["sha"], "source": row["source"],
                          "url": f"{quiz.BASE_URL}/q/{row['id']}"}, indent=2))
    elif args.command == "list":
        quiz.init()
        print(json.dumps(quiz.list_quizzes(), indent=2))
    else:
        serve(args.host, args.port, args.poll, args.profile)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, httpx.HTTPError) as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)
