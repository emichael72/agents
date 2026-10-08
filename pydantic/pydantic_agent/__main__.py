"""
Module: __main__.py

Description:
    Command-line entry point for the Pydantic Agent: `python -m pydantic_agent` from any folder
    (the package is installed in the .venv), or `python pydantic/agent.py`.
"""
import argparse
import asyncio

from pydantic_agent.session import AgentSession


def build_arg_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser for the Pydantic Agent.
    Returns:
        argparse.ArgumentParser: The parser.
    """
    parser = argparse.ArgumentParser(prog="python pydantic/agent.py",
                                     description="pydantic-ai agent that uses the shared tools.")
    parser.add_argument("--profile", help="Model profile from context/models.json (default: its \"default\").")
    parser.add_argument("--local", action="store_true", help="Shortcut for --profile local.")
    parser.add_argument("--openai", action="store_true", help="Shortcut for --profile openai.")
    parser.add_argument("--model", help="Override the profile's model for this run.")
    parser.add_argument("--base-url", help="Override the profile's OpenAI-compatible base URL for this run.")
    parser.add_argument("--prompt", help="Run one prompt and exit.")
    parser.add_argument("--history", action="store_true", help="With --prompt, print the message history.")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="Print the banner, tool calls and results as gray lines, instead of a spinner.")
    return parser


def main() -> int:
    """
    Command-line entry point: parse the options, then run the agent's session.
    Returns:
        int: The process exit code.
    """
    parser = build_arg_parser()
    args = parser.parse_args()
    if sum(map(bool, (args.profile, args.local, args.openai))) > 1:
        parser.error("use only one of --profile, --local and --openai")
    profile = args.profile or ("local" if args.local else "openai" if args.openai else None)
    session = AgentSession(trace=args.debug)
    try:
        return asyncio.run(session.run(profile, model=args.model, base_url=args.base_url, prompt=args.prompt,
                                       show_history=args.history))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
