"""
Module: __main__.py (server)

Description:
    Command-line entry point of the MCP server: `python -m mcpagent.server [server.jsonc]`, run
    from the repository root.

    Runs `MCPService` over the tools that the config names (default: server/server.jsonc next to
    this module, which serves the shared agents/tools folder). Also takes --version.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Union, Optional, Any

# Third-party
import json5
from colorama import Fore, Style, init

# Local imports
from .. import __version__
from .service import DEFAULT_CONFIG, MCPService


class ExceptionGuru:
    """
    A singleton utility class for capturing and exposing the origin (filename and line number)
    of the innermost frame where the most recent exception occurred bty ensuring the exception context
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
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

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
        If None, the default config (server/server.jsonc next to this module) is used.
    Returns:
        int: Exit status returned by the MCP service (0 for success, nonzero for failure).
    """

    def _load_project(_json_path: Path) -> Optional[dict[str, Any]]:
        """
        Load and parse the project JSONC/JSON5 file.
        Args:
            _json_path (Path): Absolute path to the JSONC/JSON5 file.
        Returns:
            Optional[dict[str, Any]]: Parsed project dictionary, or None on failure.
        """
        try:
            with open(_json_path, "r", encoding="utf-8") as f:
                project = json5.load(f)
            return project
        except Exception as json_error:
            print(f"Failed to load {_json_path}: {json_error}")
            return None

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
        # Switch to the directory containing the project file
        os.chdir(json_path.parent)
        project_data = _load_project(json_path)

        # Instantiate and start the service
        mcp_service = MCPService(project_data=project_data)
        exit_status = mcp_service.start()

    finally:
        # Always restore original CWD
        os.chdir(old_cwd)

    return exit_status


def build_arg_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser for the server (python -m mcpagent.server).
    Returns:
        argparse.ArgumentParser: The parser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m mcpagent.server",
        description="Run the MCP server over the tools that its config names.")
    parser.add_argument("config", nargs="?", type=Path, metavar="SERVER_JSONC",
                        help="Server config file (default: mcpagent/server/server.jsonc).")
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
        print(f"\n\n{Fore.YELLOW}Interrupted by user, shutting down.{Style.RESET_ALL}\n")

    except Exception as runtime_error:
        # Retrieve information about the original exception that triggered this handler.
        file_name, line_number = ExceptionGuru().get_context()
        invocation = " ".join(sys.argv)
        print(f"\n{Fore.RED}Exception:{Style.RESET_ALL} {runtime_error}.\nFile: {file_name}\nLine: {line_number}")
        print(f"Invocation: {invocation}\n")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
