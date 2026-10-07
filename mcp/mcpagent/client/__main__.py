"""
Module: __main__.py (client)

Description:
    Command-line entry point of the MCP Agent: `python -m mcpagent.client`, run from the
    mcp project directory.

    Starts the agent (`run_agent`): a model that uses the tools of the MCP servers in the client
    config (default: jsons/client.jsonc in this package), with a model profile from the
    shared agents/context/models.json. Runs one prompt (--prompt) or an interactive session.
    Also takes --version.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

# Third-party
from colorama import Fore, Style, init

# Local imports
from .. import __version__
from .agent import run_agent
from .client import DEFAULT_CONFIG


# Both CLI entry points keep their exception reporting self-contained.
# noinspection DuplicatedCode
class ExceptionGuru:
    """
    A singleton utility class for capturing and exposing the origin (filename and line number)
    of the innermost frame where the most recent exception occurred, ensuring the exception context
    is captured only once.
    """

    _instance: Optional["ExceptionGuru"] = None
    _context_stored: bool = False

    def __new__(cls) -> "ExceptionGuru":
        """
        Overrides object instantiation to implement the singleton pattern.
        Returns:
            ExceptionGuru: The singleton instance of the class.
        """
        instance = cls._instance
        if instance is None:
            instance = super().__new__(cls)
            cls._instance = instance
        return instance

    def __init__(self) -> None:
        """
        Initializes the exception context (filename and line number).
        The context is captured only once during the lifetime of the singleton instance.
        """
        if not self.__class__._context_stored:
            self._file_name: str = "<unknown>"
            self._line_number: int = -1
            self._store_context()
            self.__class__._context_stored = True

    def get_context(self) -> tuple[str, int]:
        """
        Retrieves the exception origin information.
        Returns:
            Tuple[str, int]: A tuple containing the base filename and the line number
                             where the exception originally occurred.
        """
        return self._file_name, self._line_number

    def _store_context(self) -> None:
        """
        Captures the filename and line number of the innermost frame where the most recent
        exception occurred. If no exception context is found, defaults to '<unknown>' and -1.
        """
        _exc_type, _exc_obj, exc_tb = sys.exc_info()

        if exc_tb is None:
            return

        # Traverse to the innermost (deepest) frame
        tb = exc_tb
        while tb.tb_next:
            tb = tb.tb_next

        self._file_name = os.path.basename(tb.tb_frame.f_code.co_filename)
        self._line_number = tb.tb_lineno


def build_arg_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser for the agent (python -m mcpagent.client).
    Returns:
        argparse.ArgumentParser: The parser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m mcpagent.client",
        description="Chat with a model that can call the MCP servers' tools.")
    parser.add_argument("--config", type=Path,
                        help="Client config file: MCP servers, model profiles and instructions "
                             "(default: mcp/mcpagent/jsons/client.jsonc).")
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
        # Retrieve information about the original exception that triggered this handler.
        file_name, line_number = ExceptionGuru().get_context()
        invocation = " ".join(sys.argv)
        print(f"\n{Fore.RED}Exception:{Style.RESET_ALL} {runtime_error}.\nFile: {file_name}\nLine: {line_number}")
        print(f"Invocation: {invocation}\n")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
