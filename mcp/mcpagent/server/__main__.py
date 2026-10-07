"""
Module: __main__.py (server)

Description:
    Command-line entry point of the MCP server: `python -m mcpagent.server [mcpagent.json]` from
    any folder (the package is installed in the .venv), or `python mcp/server.py`.

    Runs `MCPService` with the "server" section of the MCPAgent config (default:
    jsons/mcpagent.json in this package, which serves the shared agents/tools folder).
    Also takes --version.
"""

import argparse
from pathlib import Path

# Third-party
from rich.console import Console

# Local imports
from mcpagent.common.errors import ExceptionReport
from mcpagent import __version__
from mcpagent.server.service import MCPService


def build_arg_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser for the server (python mcp/server.py).
    Returns:
        argparse.ArgumentParser: The parser.
    """
    parser = argparse.ArgumentParser(
        prog="python mcp/server.py",
        description="Run the MCP server over the tools that its config names.")
    parser.add_argument("config", nargs="?", type=Path, metavar="CONFIG",
                        help="MCPAgent config file (default: mcp/mcpagent/jsons/mcpagent.json).")
    parser.add_argument("-v", "--version", action="store_true", help="Show the package version and exit.")
    return parser


def main() -> int:
    """
    The server's entry point.
    Returns:
        int: Exit code (0 = success, nonzero = failure).
    """
    exit_code: int = 1

    try:
        args = build_arg_parser().parse_args()
        if args.version:
            print(f"mcpagent {__version__}")
            return 0

        config_file = None
        if args.config is not None:
            config_file = args.config.expanduser().resolve()
            if not config_file.is_file():
                raise FileNotFoundError(f"Server config file not found: {config_file}")
        exit_code = MCPService.serve(config_file)

    except KeyboardInterrupt:
        Console(highlight=False).print("\n\nInterrupted by user, shutting down.\n", style="bright_black",
                                       markup=False)

    except Exception as runtime_error:
        ExceptionReport(runtime_error).print()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
