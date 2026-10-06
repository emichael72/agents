#!/usr/bin/env python3
"""
Module: ls.py

Description:
    Lists a folder for the agents: folders first, then files, with sizes and modification dates.

    Only the folders named in tools/allowed_paths.json, and folders inside them, can be listed
    (see tools/allowed_paths.py). A path starts with one of those names: `core_dump/src` lists
    `src` in the folder named core_dump. Without a path, the tool lists the allowed names.
    Output stops after MAX_ENTRIES entries.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

MAX_ENTRIES = 200

# The shared allowed-paths rule lives in the tools folder
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import allowed_paths  # noqa: E402


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
    if not args.path:
        return allowed_paths.describe()
    return listing(*allowed_paths.resolve(args.path, "dir"))


if __name__ == "__main__":
    try:
        print(main())
    except (ValueError, OSError) as e:
        print(f"Error: {e}")
        sys.exit(1)
