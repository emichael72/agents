"""
Module: __main__.py (client)

Description:
    Command-line entry point of the MCP Agent: `python -m mcpagent.client`, run from the
    mcp project directory.

    Starts the agent (`run_agent`): a model that uses the tools of the MCP servers in the
    "client" section of the MCPAgent config (default: jsons/mcpagent.jsonc in this package),
    with a model profile from the shared agents/context/models.json. Runs one prompt
    (--prompt) or an interactive session.
    Also takes --version.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

# Third-party
from colorama import Fore, Style, init

# Local imports
from mcpagent.common.errors import ExceptionReport
from .. import __version__
from .agent import run_agent
from mcpagent.config import DEFAULT_CONFIG


def build_arg_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser for the agent (python mcp/client.py).
    Returns:
        argparse.ArgumentParser: The parser.
    """
    parser = argparse.ArgumentParser(
        prog="python mcp/client.py",
        description="Chat with a model that can call the MCP servers' tools.")
    parser.add_argument("--config", type=Path,
                        help="MCPAgent config file; its client section names the MCP servers, model "
                             "profiles and instructions (default: mcp/mcpagent/jsons/mcpagent.jsonc).")
    parser.add_argument("--context", type=Path,
                        help="Text file with extra instructions for the assistant.")
    parser.add_argument("--profile",
                        help="Model profile from the models file (default: its \"default\").")
    parser.add_argument("--local", action="store_true", help="Shortcut for --profile local.")
    parser.add_argument("--openai", action="store_true", help="Shortcut for --profile openai.")
    parser.add_argument("--model", help="Override the profile's model for this run.")
    parser.add_argument("--base-url", help="Override the profile's OpenAI-compatible base URL for this run.")
    parser.add_argument("--prompt", help="Run one prompt and exit.")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="Print the banner, tool calls and results as gray lines, instead of a spinner.")
    parser.add_argument("-v", "--version", action="store_true", help="Show the package version and exit.")
    return parser


def main() -> int:
    """
    The agent's entry point.
    Returns:
        int: Exit code (0 = success, nonzero = failure).
    """
    init(autoreset=True)  # Init colorama
    exit_code: int = 1

    try:
        parser = build_arg_parser()
        args = parser.parse_args()
        if sum(map(bool, (args.profile, args.local, args.openai))) > 1:
            parser.error("use only one of --profile, --local and --openai")
        if args.version:
            print(f"mcpagent {__version__}")
            return 0

        config_path: Optional[Path] = args.config or (DEFAULT_CONFIG if DEFAULT_CONFIG.is_file() else None)
        if config_path is None:
            raise FileNotFoundError("No configuration file was provided.")
        config_file = Path(config_path).expanduser().resolve()
        if not config_file.is_file():
            raise FileNotFoundError(f"Configuration file not found: {config_file}")

        profile = args.profile or ("local" if args.local else "openai" if args.openai else None)
        context = args.context.expanduser().read_text(encoding="utf-8") if args.context else ""
        return asyncio.run(run_agent(config_file, profile=profile, model=args.model, base_url=args.base_url,
                                     prompt=args.prompt, context=context, trace=args.debug))

    except KeyboardInterrupt:
        print(f"\n\n{Fore.LIGHTBLACK_EX}Interrupted by user, shutting down.{Style.RESET_ALL}\n")

    except Exception as runtime_error:
        ExceptionReport(runtime_error).print()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
