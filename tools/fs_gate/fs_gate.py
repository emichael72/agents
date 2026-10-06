#!/usr/bin/env python3
"""
Module: fs_gate.py

Description:
    The file-system gate: the one rule for which files and folders the tools may touch, shared by
    every tool that takes a path (ls, cat, ed, wc, grep, find, df, git, make, gcc, doxy). The
    allowed folders are listed in context/paths.json. This folder holds no tool.json, so the
    agents do not offer it as a tool.

    A path the model gives starts with the name of an allowed folder: `core_dump/src/pi.c` means
    `src/pi.c` inside the folder that paths.json names core_dump. Paths are resolved (`..` and
    symbolic links included) before they are checked, so nothing outside the allowed folders can
    be reached. Tools that write (ed, make, gcc) use the same rule: what they create stays inside
    the allowed folders.

    Python tools import it; Bash tools run it:
        python3 fs_gate/fs_gate.py PATH [--dir | --file]
    which prints "<absolute path><TAB><path as shown>" or "Error: ..." with exit status 1.

    FS_GATE_PATHS may name another JSON file of the same shape, for a program that runs a tool on
    its own files (mr_gate runs doxy on a downloaded pull request). The agents only pass the fixed
    "env" of a tool's manifest, so the model cannot set it.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

TOOLS_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = TOOLS_DIR.parent  # Relative allowed folders start here
CONTEXT_DIR = REPO_DIR / "context"
PATHS_FILE = CONTEXT_DIR / "paths.json"


def load_allowed(path: Optional[Path] = None) -> dict[str, Path]:
    """
    Read the allowed folders.
    Args:
        path: The JSON file; None uses FS_GATE_PATHS, then context/paths.json.
    Returns:
        dict[str, Path]: Each name and its resolved folder.
    """
    path = path or Path(os.environ.get("FS_GATE_PATHS") or PATHS_FILE)
    paths = json.loads(path.read_text(encoding="utf-8"))["paths"]
    return {name: (REPO_DIR / Path(folder).expanduser()).resolve() for name, folder in paths.items()}


def resolve(path: str, kind: str = "any", allowed: Optional[dict[str, Path]] = None) -> tuple[Path, str]:
    """
    Turn a "<name>/<sub/path>" into an existing file or folder inside the allowed folder of that name.
    Args:
        path: The path the model gave.
        kind: "dir" or "file" to require one, "any" for either, or "output" for a file that may not
            exist yet (its folder must exist, inside the allowed folder).
        allowed: The allowed folders; None reads them.
    Returns:
        tuple[Path, str]: The absolute path, and the path as it should be shown ("<name>/<sub/path>").
    Raises:
        ValueError: If the name is not allowed, the path leaves its folder, does not exist, or is
            not of the required kind.
    """
    allowed = load_allowed() if allowed is None else allowed
    name, _, rest = path.strip().strip("/").partition("/")
    if name not in allowed:
        raise ValueError(f"'{name}' is not an allowed folder. Allowed: {', '.join(sorted(allowed))} "
                         f"(paths start with one of these names, e.g. {sorted(allowed)[0]}/...).")
    base = allowed[name]
    target = (base / rest).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"'{path}' is outside the allowed folder '{name}'.")
    if kind == "output":
        if target.is_dir() or not target.parent.is_dir():
            raise ValueError(f"'{path}' must be a file name in an existing folder.")
    elif not target.exists():
        raise ValueError(f"'{path}' does not exist.")
    if kind == "dir" and not target.is_dir():
        raise ValueError(f"'{path}' is not a folder.")
    if kind == "file" and not target.is_file():
        raise ValueError(f"'{path}' is not a file.")
    shown = name if target == base else f"{name}/{target.relative_to(base)}"
    return target, shown


def display(text: str, allowed: Optional[dict[str, Path]] = None) -> str:
    """
    Show the absolute paths in a command's output the way the model gives them (<name>/...).
    Args:
        text: The output, e.g. a compiler's messages.
        allowed: The allowed folders; None reads them.
    Returns:
        str: The text, with each allowed folder's absolute path replaced by its name.
    """
    allowed = load_allowed() if allowed is None else allowed
    for name, folder in sorted(allowed.items(), key=lambda item: -len(str(item[1]))):  # Deepest first
        text = text.replace(str(folder), name)
    return text


def describe(allowed: Optional[dict[str, Path]] = None) -> str:
    """
    List the allowed folders, for a tool called without a path.
    Args:
        allowed: The allowed folders; None reads them.
    Returns:
        str: One line per name and folder.
    """
    allowed = load_allowed() if allowed is None else allowed
    width = max(map(len, allowed))
    return "Allowed folders (paths start with one of these names):\n" + "\n".join(
        f"{name:<{width}}  {folder}" for name, folder in sorted(allowed.items()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check a tool path against context/paths.json.")
    parser.add_argument("path", help="<allowed name>/<sub/path>")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dir", action="store_const", const="dir", dest="kind", help="Require a folder")
    group.add_argument("--file", action="store_const", const="file", dest="kind", help="Require a file")
    args = parser.parse_args()
    try:
        absolute, shown = resolve(args.path, args.kind or "any")
    except (ValueError, OSError) as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"{absolute}\t{shown}")
