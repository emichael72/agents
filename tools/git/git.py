#!/usr/bin/env python3
"""
Module: git.py

Description:
    Runs read-only git commands for the agents in a repository inside the allowed folders
    (context/paths.json, checked by tools/fs_gate/fs_gate.py), e.g. `git log` in core_dump.

    Key design points:
      - Only read-only subcommands (SUBCOMMANDS); `branch` and `tag` only list. Nothing is
        committed, checked out or pushed.
      - Arguments that could read outside the repository or run another program are refused
        (FORBIDDEN): --no-index, -C, -c, --git-dir, --work-tree, --output, --ext-diff, pagers.
      - git runs in the given folder without a pager or credential prompts; the output is cut to
        MAX_LINES lines and shows paths as <allowed name>/...
"""

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

# The shared path gate (context/paths.json) lives in tools/fs_gate
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fs_gate"))
import fs_gate  # noqa: E402

SUBCOMMANDS = {"status", "log", "show", "diff", "blame", "ls-files", "shortlog", "grep", "describe",
               "rev-parse", "branch", "tag"}
LIST_ONLY = {  # With other arguments, branch and tag create, rename or delete
    "branch": {"-a", "-r", "-v", "-vv", "--all", "--remotes", "--list", "-l", "--show-current",
               "--merged", "--no-merged"},
    "tag": {"-l", "--list", "-n"},
}
FORBIDDEN = ("--no-index", "-C", "-c", "--config-env", "--git-dir", "--work-tree", "--namespace",
             "--exec-path", "--output", "--ext-diff", "--textconv", "-O", "--open-files-in-pager",
             "--upload-pack", "--receive-pack", "--paginate")
TIMEOUT = 25
MAX_LINES = 200


def run_git(path: str, command: str, args: str = "") -> tuple[bool, str]:
    """
    Run one read-only git command in an allowed folder.
    Args:
        path: <allowed name>/<folder> inside a git repository.
        command: The subcommand, e.g. log or status.
        args: Its arguments, as one string (quoted like a shell command line).
    Returns:
        tuple[bool, str]: Whether git succeeded, and its output (or error).
    Raises:
        ValueError: If the folder is not allowed, the subcommand is not read-only, or an argument is refused.
    """
    folder, shown = fs_gate.resolve(path, "dir")
    if command not in SUBCOMMANDS:
        raise ValueError(f"'git {command}' is not allowed. Allowed: {', '.join(sorted(SUBCOMMANDS))} (read-only).")
    try:
        arguments = shlex.split(args)
    except ValueError as e:
        raise ValueError(f"Cannot parse the arguments: {e}") from None
    for argument in arguments:
        if any(argument == f or argument.startswith(f + "=") or (len(f) == 2 and argument.startswith(f))
               for f in FORBIDDEN):
            raise ValueError(f"The argument '{argument}' is not allowed.")
        if command in LIST_ONLY and argument not in LIST_ONLY[command]:
            raise ValueError(f"'git {command}' may only list; allowed: {', '.join(sorted(LIST_ONLY[command]))}.")

    # No pager, editor or credential prompt; everything else from the caller's environment
    env = {**os.environ, "GIT_PAGER": "cat", "PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}
    try:
        result = subprocess.run(["git", "--no-pager", command, *arguments], cwd=folder, capture_output=True,
                                text=True, timeout=TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return False, f"git {command} in {shown}: stopped after {TIMEOUT}s"
    lines = fs_gate.display(result.stdout + result.stderr).rstrip().splitlines()
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES] + [f"... {len(lines) - MAX_LINES} more lines; narrow the command to see them"]
    if result.returncode != 0:
        return False, "\n".join([f"git {command} in {shown} failed (exit {result.returncode})", *lines])
    return True, "\n".join(lines) if lines else f"git {command} in {shown}: no output"


def main(argv: Optional[list[str]] = None) -> tuple[bool, str]:
    """
    Parse the command line and run git.
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        tuple[bool, str]: Whether git succeeded, and its output.
    """
    parser = argparse.ArgumentParser(description="Run a read-only git command in an allowed folder.")
    parser.add_argument("path", help="<allowed name>/<folder> in a repository, e.g. core_dump")
    parser.add_argument("command", help=f"One of: {', '.join(sorted(SUBCOMMANDS))}")
    parser.add_argument("--args", default="", help='Arguments, e.g. "-5 --oneline"')
    argv = sys.argv[1:] if argv is None else argv
    # The agents pass "--args", "-5 --oneline": join them, or argparse takes -5 for an option
    argv = [f"{a}={b}" if a == "--args" and i + 1 < len(argv) else a
            for i, (a, b) in enumerate(zip(argv, argv[1:] + [""]))
            if not (i > 0 and argv[i - 1] == "--args")]
    args = parser.parse_args(argv)
    return run_git(args.path, args.command, args.args)


if __name__ == "__main__":
    try:
        ok, report = main()
    except (ValueError, OSError) as e:
        ok, report = False, f"Error: {e}"
    print(report)
    sys.exit(0 if ok else 1)
