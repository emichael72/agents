#!/usr/bin/env python3
"""
Module: grep.py

Description:
    Searches file contents with GNU grep for the agents, in a file or folder (recursively) inside the
    allowed folders (context/paths.json, checked by tools/fs_gate/fs_gate.py).

    Key design points:
      - The pattern is passed with -e and the path after --, so neither can be read as an option.
        Every other option is set by this script from typed parameters.
      - Skips binary files and the .git, .venv, node_modules and __pycache__ folders.
      - At most MAX_MATCHES matching lines (or files, with files_only); paths are shown as
        <allowed name>/...
"""

import subprocess
import sys
from pathlib import Path
from typing import Optional

# The shared path gate (context/paths.json) lives in tools/fs_gate
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fs_gate"))
import fs_gate  # noqa: E402

SKIPPED_DIRS = (".git", ".venv", "node_modules", "__pycache__")
OPTIONS = ("path", "ignore_case", "fixed", "files_only", "context", "include")
MAX_MATCHES = 50
TIMEOUT = 25


def grep(pattern: str, path: str = "tools", ignore_case: bool = False, fixed: bool = False,
         files_only: bool = False, context: int = 0, include: Optional[str] = None) -> str:
    """
    Search for a pattern.
    Args:
        pattern: An extended regular expression, or plain text with fixed.
        path: <allowed name>/<file or folder>.
        ignore_case: Match regardless of case.
        fixed: Treat the pattern as plain text, not a regular expression.
        files_only: List the matching files instead of the lines.
        context: Lines to show around each match, 0 to 5.
        include: Only search files whose names match this glob, e.g. *.c.
    Returns:
        str: The matches as file:line:text (or file names), or a "No matches" line.
    Raises:
        ValueError: If the pattern is empty, the path is not allowed, or an option is out of range.
    """
    if not pattern:
        raise ValueError("Give a pattern to search for.")
    if not 0 <= context <= 5:
        raise ValueError("context must be 0 to 5 lines.")
    target, shown = fs_gate.resolve(path)
    command = ["grep", "-r", "-I", "-H", "-F" if fixed else "-E"]
    command += ["-i"] * ignore_case + (["-l"] if files_only else ["-n"])
    command += [f"-C{context}"] * (context > 0 and not files_only)
    command += [f"--exclude-dir={name}" for name in SKIPPED_DIRS]
    command += [f"--include={include}"] if include else []
    command += ["-e", pattern, "--", str(target)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ValueError(f"The search took over {TIMEOUT}s; narrow the path or the pattern.") from None
    if result.returncode == 1:
        return f"No matches for '{pattern}' in {shown}" + (f" ({include})" if include else "")
    if result.returncode != 0:
        raise ValueError(fs_gate.display(result.stderr.strip()) or "grep failed")
    lines = fs_gate.display(result.stdout).rstrip("\n").splitlines()
    unit = "files" if files_only else "lines"
    if len(lines) > MAX_MATCHES:
        return "\n".join(lines[:MAX_MATCHES] + [f"... {len(lines)} {unit} in total; narrow the pattern, "
                                                f"the path or include to see the rest"])
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> str:
    """
    Read "<pattern> [--option value ...]" and search. Values are taken verbatim, even when they
    start with "-", as the agents pass them.
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        str: The search result.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    options: dict = {}
    while argv:
        argument = argv.pop(0)
        if argument.startswith("--") and argument[2:] in OPTIONS and argv:
            options[argument[2:]] = argv.pop(0)
        elif "pattern" not in options:
            options["pattern"] = argument
        else:
            raise ValueError(f"Unexpected argument '{argument}'.")

    def flag(name: str) -> bool:
        return options.get(name, "").lower() in ("true", "1", "yes")

    try:
        context = int(options.get("context", 0))
    except ValueError:
        raise ValueError("context must be a whole number.") from None
    return grep(options.get("pattern", ""), options.get("path", "tools"), flag("ignore_case"), flag("fixed"),
                flag("files_only"), context, options.get("include"))


if __name__ == "__main__":
    try:
        print(main())
    except (ValueError, OSError) as e:
        print(f"Error: {e}")
        sys.exit(1)
