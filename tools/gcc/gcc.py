#!/usr/bin/env python3
"""
Module: gcc.py

Description:
    Compiles C sources with gcc for the agents. Sources, include folders and the output must all be
    inside the allowed folders (context/paths.json, checked by tools/fs_gate/fs_gate.py).

    Without an output, gcc only checks the sources (-fsyntax-only): the result is the warnings and
    errors. With an output, it compiles and links a program there.

    Key design points:
      - Flags come from a short list (ALLOWED_FLAGS): warnings, optimization, debug info, the C
        standard, defines, libraries and include folders (-I<allowed name>/<folder>). Anything that
        could write elsewhere or load code (-o, -fplugin, -B, -wrapper, @file, specs) is refused.
      - Stops after TIMEOUT seconds and shows the last MAX_LINES lines of gcc's messages, with paths
        shown as <allowed name>/...
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# The shared path gate (context/paths.json) lives in tools/fs_gate
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fs_gate"))
import fs_gate  # noqa: E402

SOURCE_EXTENSIONS = {".c", ".h"}
ALLOWED_FLAGS = re.compile(r"""^-(
    W[a-z0-9-]+(=[a-z0-9]+)?        # Warnings: -Wall, -Wextra, -Werror, -Wno-unused
  | O[0-3sgz]?                      # Optimization
  | g[0-3]?                         # Debug info
  | std=[a-z0-9+]+                  # C standard: -std=c11
  | pedantic(-errors)?
  | D[A-Za-z_][A-Za-z0-9_]*(=[A-Za-z0-9_.]*)?   # Defines: -DNDEBUG, -DLEVEL=2
  | l[a-z0-9_+]+                    # Libraries: -lm
)$""", re.VERBOSE)
TIMEOUT = 25
MAX_LINES = 60


def compile_sources(sources: str, output: Optional[str] = None, flags: str = "") -> tuple[bool, str]:
    """
    Check or build C sources.
    Args:
        sources: One or more .c files (headers for a check), separated by spaces, each starting with
            an allowed folder name.
        output: The program to build, inside an allowed folder; None only checks the sources.
        flags: gcc flags separated by spaces, from ALLOWED_FLAGS or -I<allowed folder>.
    Returns:
        tuple[bool, str]: Whether gcc succeeded, and a summary line followed by its messages.
    Raises:
        ValueError: If a path is not allowed or not a C file, or a flag is not allowed.
    """
    files = []
    for path in sources.split():
        target, shown = fs_gate.resolve(path, "file")
        if target.suffix not in SOURCE_EXTENSIONS:
            raise ValueError(f"'{shown}' is not a C source or header (.c, .h).")
        files.append(target)
    if not files:
        raise ValueError("Give at least one C source, e.g. core_dump/src/main.c.")

    arguments = []
    for flag in flags.split():
        if flag.startswith("-I") and len(flag) > 2:
            arguments.append("-I" + str(fs_gate.resolve(flag[2:], "dir")[0]))
        elif ALLOWED_FLAGS.match(flag):
            arguments.append(flag)
        else:
            raise ValueError(f"The flag '{flag}' is not allowed. Allowed: -W..., -O..., -g, -std=..., "
                             "-pedantic, -D..., -l..., -I<allowed folder>.")

    if output:
        target, shown_output = fs_gate.resolve(output, "output")
        command = ["gcc", *arguments, *map(str, files), "-o", str(target)]
        action = f"build {shown_output}"
    else:
        command = ["gcc", "-fsyntax-only", *arguments, *map(str, files)]
        action = "check"
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"gcc {action}: stopped after {TIMEOUT}s"
    lines = fs_gate.display(result.stdout + result.stderr).rstrip().splitlines()
    if len(lines) > MAX_LINES:
        lines = [f"... {len(lines) - MAX_LINES} earlier lines not shown"] + lines[-MAX_LINES:]
    warnings = sum(": warning:" in line for line in lines)
    if result.returncode == 0:
        summary = f"gcc {action}: succeeded, {warnings} warning(s), {len(files)} file(s)"
    else:
        summary = f"gcc {action}: failed (exit {result.returncode}), {len(files)} file(s)"
    return result.returncode == 0, "\n".join([summary, *lines])


def main(argv: Optional[list[str]] = None) -> tuple[bool, str]:
    """
    Parse the command line and run gcc.
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        tuple[bool, str]: Whether gcc succeeded, and its report.
    """
    parser = argparse.ArgumentParser(description="Check or build C sources inside the allowed folders.")
    parser.add_argument("sources", help="C files separated by spaces, e.g. core_dump/src/main.c")
    parser.add_argument("--output", help="Program to build, e.g. core_dump/hello; omit it to only check")
    parser.add_argument("--flags", default="", help="gcc flags separated by spaces, e.g. -Wall -std=c11 -lm")
    argv = sys.argv[1:] if argv is None else argv
    # The agents pass "--flags", "-Wall": join them, or argparse takes -Wall for an option
    argv = [f"{a}={b}" if a in ("--flags", "--output") and i + 1 < len(argv) else a
            for i, (a, b) in enumerate(zip(argv, argv[1:] + [""]))
            if not (i > 0 and argv[i - 1] in ("--flags", "--output"))]
    args = parser.parse_args(argv)
    return compile_sources(args.sources, args.output, args.flags)


if __name__ == "__main__":
    try:
        ok, report = main()
    except (ValueError, OSError) as e:
        ok, report = False, f"Error: {e}"
    print(report)
    sys.exit(0 if ok else 1)
