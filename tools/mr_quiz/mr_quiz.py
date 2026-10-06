#!/usr/bin/env python3
"""
Module: mr_quiz.py

Description:
    Command line for the developer quiz, and the entry point the agents run as the `mr_quiz` tool.

    Commands:
      - status [--pr N]: The open PRs and their quiz state, with links (the agents' tool).
      - create PR [--profile NAME] [--fixed FILE]: Create (or re-post) the quiz for a PR now.
      - list: Every stored quiz, as JSON.
      - serve [--host --port --poll --profile]: Run the web service and the GitHub poller.
"""

import argparse
import json
import logging
import sys
from typing import Optional

# Third-party
import httpx

# Local imports
import quiz


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
    try:
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=3).raise_for_status()
        lines = [f"Quiz service: running at {quiz.BASE_URL}"]
    except httpx.HTTPError:
        lines = ["Quiz service: not running, so new commits get no quiz (systemctl --user start mr-quiz)"]

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
                state = "no quiz yet; the service creates one within a minute or two"
            elif not row["docs_ok"]:
                state = f"documentation problems, merge blocked: {quiz.BASE_URL}/q/{row['id']}"
            elif row["cosmetic"]:
                state = "cosmetic change (comments/formatting only), documentation OK, may merge"
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
    logging.getLogger("mr_quiz").info("Quizzes for %s by %s, model %s / %s, links at %s",
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

    create_cmd = sub.add_parser("create", help="Create (or re-post) the quiz for a PR's current revision")
    create_cmd.add_argument("pr", type=int)
    create_cmd.add_argument("--profile", help="Model profile from context/models.json (local or openai)")
    create_cmd.add_argument("--fixed", help="Use a JSON quiz file instead of the model")

    sub.add_parser("list", help="List every stored quiz as JSON")

    serve_cmd = sub.add_parser("serve", help="Run the web service and the GitHub poller")
    serve_cmd.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
    serve_cmd.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
    serve_cmd.add_argument("--poll", type=float, default=30, help="Seconds between GitHub polls; 0 disables")
    serve_cmd.add_argument("--profile", help="Model profile from context/models.json (local or openai)")

    args = parser.parse_args(argv)
    if args.command == "status":
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
