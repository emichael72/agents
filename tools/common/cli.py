"""
Module: cli.py

Description:
    The command line every Python tool shares. The agents call a tool as
        python3 <tool>/<tool>.py --<name>=<value> ... [-- <positional> ...]
    (tools/README.md, "Parameters"): each option and its value in one argument, so a value may start
    with "-" or span lines, and the positional values after "--", so none is taken for an option.

    `ToolArgumentParser` reads that with argparse. A mistake on the command line raises ValueError,
    which the tool prints as "Error: <reason>" with exit status 1, like any other failure, rather
    than argparse's usage text and status 2. -v/--version prints the tool's version.

    It runs under the system's python3 (3.9 on RHEL 9), outside the agents' .venv.
"""

import argparse
from typing import NoReturn


class ToolArgumentParser(argparse.ArgumentParser):
    """
    argparse for a tool: errors raise ValueError, and -v/--version is built in.
    """

    def __init__(self, prog: str, version: str, description: str) -> None:
        """
        Args:
            prog: The tool's name, as -v and the usage show it.
            version: The tool's version.
            description: What the tool does, for --help.
        """
        super().__init__(prog=prog, description=description, allow_abbrev=False)
        self.add_argument("-v", "--version", action="version", version=f"{prog} {version}")

    def error(self, message: str) -> NoReturn:
        """
        Report a command-line mistake.
        Args:
            message: What is wrong, as argparse words it.
        Raises:
            ValueError: Always, with the message.
        """
        raise ValueError(message)

    @staticmethod
    def boolean(value: str) -> bool:
        """
        Read a boolean parameter as the agents pass it: "true" or "True" (also "1" or "yes") is true,
        anything else false.
        Args:
            value: The value.
        Returns:
            bool: The flag.
        """
        return value.strip().lower() in ("true", "1", "yes")
