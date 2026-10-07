"""
Module: __main__.py (server)

Description:
    Command-line entry point of the MCP server: `python -m mcpagent.server [mcpagent.json]`, run
    from the mcp project directory.

    Runs `MCPService` with the "server" section of the MCPAgent config (default:
    jsons/mcpagent.json in this package, which serves the shared agents/tools folder).
    Also takes --version.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Union, Optional

# Third-party
from colorama import Fore, Style, init

# Local imports
from mcpagent.common.errors import ExceptionReport
from mcpagent import MCPService, __version__
from mcpagent.config import DEFAULT_CONFIG, REPO_ROOT, MCPAgentConfig


def start_mcp_server(config_path: Optional[Union[str, Path]] = None) -> int:
    """
    Entry point to launch an MCP server.
    This function loads a project configuration file (JSON),
    instantiates an `MCPService`, and starts the service.
    Args:
        config_path (Optional[Union[str, Path]]): Path to the project configuration file.
        Supports environment variables and '~' expansion.
        If None, the default config (mcp/mcpagent/jsons/mcpagent.json) is used.
    Returns:
        int: Exit status returned by the MCP service (0 for success, nonzero for failure).
    """

    if config_path is None:
        config_path = DEFAULT_CONFIG if DEFAULT_CONFIG.is_file() else None
    if config_path is None:
        raise Exception("No configuration file specified")

    expanded = os.path.expanduser(os.path.expandvars(str(config_path)))
    json_path = Path(expanded).resolve()
    if not json_path.is_file():
        raise RuntimeError(f"Project file not found: {json_path}")

    old_cwd = Path.cwd()
    try:
        # Paths in the config (tools_dir) are relative to the repository root
        os.chdir(REPO_ROOT)
        project_data = MCPAgentConfig.load(json_path).server

        # Instantiate and start the service
        mcp_service = MCPService(project_data=project_data)
        exit_status = mcp_service.start()

    finally:
        # Always restore original CWD
        os.chdir(old_cwd)

    return exit_status


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
    init(autoreset=True)  # Init colorama
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
        exit_code = start_mcp_server(config_file)

    except KeyboardInterrupt:
        print(f"\n\n{Fore.LIGHTBLACK_EX}Interrupted by user, shutting down.{Style.RESET_ALL}\n")

    except Exception as runtime_error:
        ExceptionReport(runtime_error).print()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
