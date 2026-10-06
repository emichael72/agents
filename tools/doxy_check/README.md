# Doxy Check

Checks that C/C++ sources and headers are documented with Doxygen: every file has a `@file`
block, and every function, parameter and return value is described. Read-only.

**Usage Example:**

```bash
bash doxy_check/doxy_check.sh ~/projects/core_dump/src
bash doxy_check/doxy_check.sh "src/pi.c include/pi.h"   # several paths in one argument, as the agents pass them
```

Each path is a file (`.c`, `.h`, `.cc`, `.cpp`, `.hpp`, `.cxx`, `.hh`) or a folder, searched
recursively. Paths are relative to the tools folder, or absolute; a leading `~` is expanded.

## Result

Either `All documented: N file(s) checked, ...`, or the problems, one per line as
`file:line: message` (at most 100 lines):

```text
Documentation problems: 3 in 6 file(s) checked (doxygen 1.16.1, Doxyfile.check):
src/modules/pi.c:1: error: File has no @file documentation block, so Doxygen does not check its contents.
src/math.c:6: error: Member subtract(int a, int b) (function) of file math.c is not documented.
src/math.c:13: error: The following parameter of multiply(int a, int b) is not documented:
  parameter 'b'
```

Documentation problems are the tool's result, so it exits 0. It exits nonzero only when the check
cannot run: Doxygen is missing, a path does not exist or is not C/C++, or Doxygen itself fails.

## Settings

[`Doxyfile.check`](Doxyfile.check) holds the Doxygen settings; edit it to change what is checked.
The script keeps every setting except these, which it overrides:

| Setting | Override | Why |
| --- | --- | --- |
| `INPUT` | The paths given to the tool | The tool checks what it is asked to |
| `OUTPUT_DIRECTORY`, `WARN_LOGFILE` | A temporary folder, deleted afterwards | Nothing is written next to the sources |
| `GENERATE_XML` | `YES` | Doxygen refuses to run with no output format |
| `WARN_FORMAT` | `$file:$line: $text` | One parseable format |

With `EXTRACT_ALL = NO`, Doxygen ignores everything in a file that has no `@file` block, without
a warning. The script therefore also reports each such file itself.

Requires Doxygen (`sudo dnf install doxygen` on Fedora, `sudo apt install doxygen` on Debian/Ubuntu).
