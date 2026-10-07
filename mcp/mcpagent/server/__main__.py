"""
Module: __main__.py (server)

Description:
    Command-line entry point of the MCP server: `python -m mcpagent.server [mcpagent.jsonc]`, run
    from the mcp project directory.

    Runs `MCPService` with the "server" section of the MCPAgent config (default:
    jsons/mcpagent.jsonc in this package, which serves the shared agents/tools folder).
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
from mcpagent import MCPService, __version__
from mcpagent.config import DEFAULT_CONFIG, REPO_ROOT, load_config, server_settings


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


def start_mcp_server(config_path: Optional[Union[str, Path]] = None) -> int:
    """
    Entry point to launch an MCP server.
    This function loads a project configuration file (JSON/JSONC/JSON5),
    instantiates an `MCPService`, and starts the service.
    Args:
        config_path (Optional[Union[str, Path]]): Path to the project configuration file.
        Supports environment variables and '~' expansion.
        If None, the default config (mcp/mcpagent/jsons/mcpagent.jsonc) is used.
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
        project_data = server_settings(load_config(json_path), json_path)

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
                        help="MCPAgent config file (default: mcp/mcpagent/jsons/mcpagent.jsonc).")
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
        # Retrieve information about the original exception that triggered this handler.
        file_name, line_number = ExceptionGuru().get_context()
        invocation = " ".join(sys.argv)
        print(f"\n{Fore.RED}Exception:{Style.RESET_ALL} {runtime_error}.\nFile: {file_name}\nLine: {line_number}")
        print(f"Invocation: {invocation}\n")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
