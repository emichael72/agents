# Common tool code

Code shared by the Python tools. This folder has no `tool.json`, so the agents do not offer it as a tool.

## Command-line parser

`cli.py` provides `ToolArgumentParser`, the argparse parser behind every Python tool. It reads the calling convention
the agents use: each option as `--name=value`, then the positional values after `--`.

~~~bash
python3 tools/ed/ed.py --old=-Wall --new='-Wall -Wextra' -- core_dump/Makefile
python3 tools/ed/ed.py --version
~~~

The parser differs from plain argparse in three ways:

- A command-line mistake raises `ValueError`. The tool prints it as `Error: <reason>` and exits with status 1, as it
  does for any other failure, instead of printing argparse's usage text and exiting with status 2.
- `-v` and `--version` print the tool's name and version.
- `ToolArgumentParser.boolean` reads a boolean parameter: `true`, `1`, or `yes` in any case is true; anything else is
  false.

## Using it in a tool

A tool finds the repository root, the nearest folder above it holding `pyproject.toml`, and imports from there:

~~~python
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents
                            if (p / "pyproject.toml").is_file())))
from tools.common.cli import ToolArgumentParser

parser = ToolArgumentParser("ed", "1.0.0", "Edit a text file in the allowed folders.")
parser.add_argument("path", nargs="?")
parser.add_argument("--all", type=ToolArgumentParser.boolean, default=False)
~~~

The tools run under the system's `python3` (3.9 on RHEL 9), outside the agents' `.venv`, so this code uses only the
standard library.
