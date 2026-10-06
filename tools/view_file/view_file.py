#!/usr/bin/env python3
"""
Module: view_file.py

Description:
    Shows a text file to the agents, with line numbers, a range of lines at a time.

    Only files inside the folders named in tools/allowed_paths.json can be read (see
    tools/allowed_paths.py): a path starts with one of those names, e.g. core_dump/src/main.c.

    Key design points:
      - At most MAX_LINES lines per call; the header says which lines are shown and how many the
        file has, so the model can ask for the next range.
      - Binary files and files over MAX_BYTES are refused rather than dumped.
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

# The shared allowed-paths rule lives in the tools folder
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import allowed_paths  # noqa: E402

MAX_LINES = 200
MAX_BYTES = 2_000_000


def view(path: str, start: int = 1, count: int = MAX_LINES) -> str:
    """
    Show a range of a text file's lines, numbered.
    Args:
        path: <allowed name>/<file>.
        start: The first line to show (1-based).
        count: How many lines to show, at most MAX_LINES.
    Returns:
        str: A header ("<path>: lines A-B of N"), then each line as "<number>  <text>".
    Raises:
        ValueError: If the path is not allowed, the file is binary or too large, or the range is invalid.
    """
    if start < 1 or not 1 <= count <= MAX_LINES:
        raise ValueError(f"start must be 1 or more, and count 1 to {MAX_LINES}.")
    target, shown = allowed_paths.resolve(path, "file")
    if target.stat().st_size > MAX_BYTES:
        raise ValueError(f"'{shown}' is larger than {MAX_BYTES:,} bytes; search it with search_text instead.")
    data = target.read_bytes()
    if b"\0" in data[:8192]:
        raise ValueError(f"'{shown}' is a binary file.")
    lines = data.decode("utf-8", errors="replace").splitlines()
    if not lines:
        return f"{shown}: empty file"
    if start > len(lines):
        raise ValueError(f"'{shown}' has only {len(lines)} lines.")
    end = min(start + count - 1, len(lines))
    width = len(str(end))
    body = "\n".join(f"{n:>{width}}  {lines[n - 1]}" for n in range(start, end + 1))
    more = f"; continue with start {end + 1}" if end < len(lines) else ""
    return f"{shown}: lines {start}-{end} of {len(lines)}{more}\n{body}"


def main(argv: Optional[list[str]] = None) -> str:
    """
    Parse the command line and show the file.
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        str: The numbered lines.
    """
    parser = argparse.ArgumentParser(description="Show a text file's lines, numbered.")
    parser.add_argument("path", help="<allowed name>/<file>, e.g. core_dump/src/main.c")
    parser.add_argument("--start", type=int, default=1, help="First line to show (default 1)")
    parser.add_argument("--count", type=int, default=MAX_LINES, help=f"Lines to show, at most {MAX_LINES}")
    args = parser.parse_args(argv)
    return view(args.path, args.start, args.count)


if __name__ == "__main__":
    try:
        print(main())
    except (ValueError, OSError) as e:
        print(f"Error: {e}")
        sys.exit(1)
