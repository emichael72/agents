#!/usr/bin/env python3
"""
Module: find.py

Description:
    Finds files and folders by name with GNU find for the agents, under a folder inside the
    allowed folders (context/paths.json, checked by tools/fs_gate/fs_gate.py).

    Key design points:
      - Only name, type and depth tests, built by this script; nothing from the model reaches
        find as an option, so actions such as -exec or -delete cannot be asked for.
      - Skips the .git, .venv, node_modules and __pycache__ folders.
      - At most MAX_RESULTS paths, sorted, shown as <allowed name>/... (folders end with /).
"""

import subprocess
import sys
from pathlib import Path
from typing import Optional

# The shared path gate (context/paths.json) lives in tools/fs_gate
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fs_gate"))
import fs_gate  # noqa: E402

SKIPPED_DIRS = (".git", ".venv", "node_modules", "__pycache__")
OPTIONS = ("name", "type", "max_depth", "ignore_case")
MAX_RESULTS = 200
TIMEOUT = 25


def find(path: str, name: Optional[str] = None, kind: Optional[str] = None,
         max_depth: Optional[int] = None, ignore_case: bool = False) -> str:
    """
    Find files and folders under a folder.
    Args:
        path: <allowed name>/<folder> to search under.
        name: A glob for the name, e.g. *.c or Makefile; None matches everything.
        kind: "f" for files, "d" for folders; None for both.
        max_depth: How many folder levels to descend, 1 to 20; None for all.
        ignore_case: Match the name regardless of case.
    Returns:
        str: One path per line, or a "Nothing found" line.
    Raises:
        ValueError: If the path is not allowed or an option is out of range.
    """
    folder, shown = fs_gate.resolve(path, "dir")
    if kind not in (None, "f", "d"):
        raise ValueError("type must be f (files) or d (folders).")
    if max_depth is not None and not 1 <= max_depth <= 20:
        raise ValueError("max_depth must be 1 to 20.")
    command = ["find", str(folder), "-mindepth", "1"]
    command += ["-maxdepth", str(max_depth)] if max_depth else []
    # Prune the skipped folders, then print what matches the tests
    prune = []
    for skipped in SKIPPED_DIRS:
        prune += (["-o"] if prune else []) + ["-name", skipped]
    command += ["(", "-type", "d", "(", *prune, ")", ")", "-prune", "-o", "("]
    command += ["-type", kind] if kind else ["-true"]
    command += (["-iname" if ignore_case else "-name", name] if name else [])
    command += [")", "-printf", "%p%y\n"]  # %y appends the type: f, d, l, ...
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ValueError(f"The search took over {TIMEOUT}s; narrow the folder or set max_depth.") from None
    if result.returncode != 0 and not result.stdout:
        raise ValueError(fs_gate.display(result.stderr.strip()) or "find failed")
    paths = sorted(line[:-1] + ("/" if line.endswith("d") else "") for line in result.stdout.splitlines() if line)
    if not paths:
        return f"Nothing found in {shown}" + (f" named {name}" if name else "")
    lines = fs_gate.display("\n".join(paths)).splitlines()
    if len(lines) > MAX_RESULTS:
        lines = lines[:MAX_RESULTS] + [f"... {len(lines)} in total; narrow the name, type or max_depth"]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> str:
    """
    Read "<path> [--option value ...]" and search. Values are taken verbatim, as the agents pass them.
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        str: The paths found.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    options: dict = {}
    while argv:
        argument = argv.pop(0)
        if argument.startswith("--") and argument[2:] in OPTIONS and argv:
            options[argument[2:]] = argv.pop(0)
        elif "path" not in options:
            options["path"] = argument
        else:
            raise ValueError(f"Unexpected argument '{argument}'.")
    if "path" not in options:
        raise ValueError("Give a folder to search, e.g. core_dump.")
    try:
        max_depth = int(options["max_depth"]) if "max_depth" in options else None
    except ValueError:
        raise ValueError("max_depth must be a whole number.") from None
    return find(options["path"], options.get("name"), options.get("type"), max_depth,
                options.get("ignore_case", "").lower() in ("true", "1", "yes"))


if __name__ == "__main__":
    try:
        print(main())
    except (ValueError, OSError) as e:
        print(f"Error: {e}")
        sys.exit(1)
