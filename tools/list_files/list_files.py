#!/usr/bin/env python3
"""
Module: list_files.py

Description:
    Lists a folder for the agents: folders first, then files, with sizes and modification dates.

    Only the folders named in allowed_paths.json (next to this script), and folders inside them,
    can be listed. A path starts with one of those names: `core_dump/src` lists `src` in the
    folder named core_dump. Without a path, the tool lists the allowed names.

    Key design points:
      - Paths are resolved (symbolic links and `..` included) before the check, so nothing outside
        an allowed folder can be reached.
      - Output stops after MAX_ENTRIES entries.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

ALLOWED_FILE = Path(__file__).resolve().parent / "allowed_paths.json"
TOOLS_DIR = Path(__file__).resolve().parent.parent  # Relative allowed paths start here
MAX_ENTRIES = 200


def load_allowed(path: Path = ALLOWED_FILE) -> dict[str, Path]:
    """
    Read the allowed folders.
    Args:
        path: The JSON file (default: list_files/allowed_paths.json).
    Returns:
        dict[str, Path]: Each name and its resolved folder.
    """
    paths = json.loads(path.read_text(encoding="utf-8"))["paths"]
    return {name: (TOOLS_DIR / Path(folder).expanduser()).resolve() for name, folder in paths.items()}


def resolve(path: str, allowed: dict[str, Path]) -> tuple[Path, str]:
    """
    Turn a "<name>/<sub/path>" into a folder inside the allowed folder of that name.
    Args:
        path: The path the model gave.
        allowed: The allowed folders, by name.
    Returns:
        tuple[Path, str]: The folder, and the path as it should be shown.
    Raises:
        ValueError: If the name is not allowed, the path leaves its folder, or it is not a folder.
    """
    name, _, rest = path.strip().strip("/").partition("/")
    if name not in allowed:
        raise ValueError(f"'{name}' is not an allowed folder. Allowed: {', '.join(sorted(allowed))}.")
    base = allowed[name]
    target = (base / rest).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"'{path}' is outside the allowed folder '{name}'.")
    if not target.is_dir():
        raise ValueError(f"'{path}' is not a folder.")
    shown = name + ("/" + str(target.relative_to(base)) if target != base else "")
    return target, shown


def human_size(size: int) -> str:
    """
    Format a byte count the way `ls -h` does.
    Args:
        size: Bytes.
    Returns:
        str: For example "512", "4.0K" or "1.2M".
    """
    value = float(size)
    for unit in ("", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{int(value)}" if not unit else f"{value:.1f}{unit}"
        value /= 1024
    return str(size)


def listing(folder: Path, shown: str) -> str:
    """
    List a folder: folders first, then files, each with its size and modification date.
    Args:
        folder: The folder.
        shown: Its name in the header.
    Returns:
        str: A header line, then one line per entry (at most MAX_ENTRIES).
    """
    entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    lines = [f"{shown} ({len(entries)} entries):"]
    for entry in entries[:MAX_ENTRIES]:
        info = entry.lstat()
        date = datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d")
        if entry.is_symlink():
            name = f"{entry.name} -> {entry.readlink()}"
        else:
            name = entry.name + ("/" if entry.is_dir() else "")
        size = "-" if entry.is_dir() and not entry.is_symlink() else human_size(info.st_size)
        lines.append(f"{date}  {size:>6}  {name}")
    if len(entries) > MAX_ENTRIES:
        lines.append(f"... and {len(entries) - MAX_ENTRIES} more")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> str:
    """
    Parse the command line and list the folder.
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        str: The listing, or the allowed folders when no path is given.
    """
    parser = argparse.ArgumentParser(description="List a folder inside the allowed folders.")
    parser.add_argument("path", nargs="?", help="<allowed name>/<sub/path>, e.g. core_dump/src")
    args = parser.parse_args(argv)
    allowed = load_allowed()
    if not args.path:
        width = max(map(len, allowed))
        return "Allowed folders (list one with <name> or <name>/<sub/path>):\n" + "\n".join(
            f"{name:<{width}}  {folder}" for name, folder in sorted(allowed.items()))
    return listing(*resolve(args.path, allowed))


if __name__ == "__main__":
    try:
        print(main())
    except (ValueError, OSError) as e:
        print(f"Error: {e}")
        sys.exit(1)
