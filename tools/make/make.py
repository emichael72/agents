#!/usr/bin/env python3
"""
Module: make.py

Description:
    Runs GNU make for the agents in a folder inside the allowed folders (tools/allowed_paths.json,
    checked by tools/allowed_paths.py), e.g. `core_dump` or `core_dump/src`.

    Key design points:
      - The target must be a plain name (all, clean, core_dump); options and VAR=value overrides
        are refused, and MAKEFLAGS from the environment is ignored, so the model cannot point make
        at another Makefile or folder.
      - make runs the commands its Makefile contains: allow only folders whose Makefiles you trust.
      - Stops after TIMEOUT seconds (under the agents' 30-second tool limit) and shows the last
        MAX_LINES lines of output, with paths shown as <allowed name>/...
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# The shared allowed-paths rule lives in the tools folder
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import allowed_paths  # noqa: E402

TARGET = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
TIMEOUT = 25
MAX_LINES = 60


def run_make(path: str, target: Optional[str] = None) -> tuple[bool, str]:
    """
    Run make in an allowed folder.
    Args:
        path: <allowed name>/<folder> holding the Makefile.
        target: The make target; None builds the default one.
    Returns:
        tuple[bool, str]: Whether make succeeded, and a summary line followed by its output.
    Raises:
        ValueError: If the folder is not allowed, has no Makefile, or the target is not a plain name.
    """
    folder, shown = allowed_paths.resolve(path, "dir")
    if not any((folder / name).is_file() for name in ("GNUmakefile", "makefile", "Makefile")):
        raise ValueError(f"'{shown}' has no Makefile.")
    if target and (not TARGET.match(target) or ".." in target):
        raise ValueError(f"'{target}' is not a plain make target (e.g. all, clean).")
    env = {key: value for key, value in os.environ.items() if not key.startswith("MAKE")}
    command = ["make", "--no-print-directory", "-C", str(folder), *([target] if target else [])]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return False, f"make {target or ''} in {shown}: stopped after {TIMEOUT}s"
    lines = allowed_paths.display(result.stdout + result.stderr).rstrip().splitlines()
    if len(lines) > MAX_LINES:
        lines = [f"... {len(lines) - MAX_LINES} earlier lines not shown"] + lines[-MAX_LINES:]
    status = "succeeded" if result.returncode == 0 else f"failed (exit {result.returncode})"
    summary = f"make {target or '(default target)'} in {shown}: {status}"
    return result.returncode == 0, "\n".join([summary, *lines] if lines else [summary, "(no output)"])


def main(argv: Optional[list[str]] = None) -> tuple[bool, str]:
    """
    Parse the command line and run make.
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        tuple[bool, str]: Whether make succeeded, and its report.
    """
    parser = argparse.ArgumentParser(description="Run make in an allowed folder.")
    parser.add_argument("path", help="<allowed name>/<folder>, e.g. core_dump")
    parser.add_argument("--target", help="Target to build, e.g. all or clean (default: the Makefile's first)")
    argv = sys.argv[1:] if argv is None else argv
    # The agents pass "--target", "<value>": join them, so a value starting with - is checked, not parsed
    argv = [f"{a}={b}" if a == "--target" and i + 1 < len(argv) else a
            for i, (a, b) in enumerate(zip(argv, argv[1:] + [""]))
            if not (i > 0 and argv[i - 1] == "--target")]
    args = parser.parse_args(argv)
    return run_make(args.path, args.target)


if __name__ == "__main__":
    try:
        ok, report = main()
    except (ValueError, OSError) as e:
        ok, report = False, f"Error: {e}"
    print(report)
    sys.exit(0 if ok else 1)
