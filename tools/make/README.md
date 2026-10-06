# Make

Runs GNU make in a folder inside the allowed folders
([`../allowed_paths.json`](../allowed_paths.json)), such as `core_dump`, and reports whether it
succeeded with the last 60 lines of output (paths shown as `<allowed name>/...`).

**Usage Example:**

```bash
python3 make/make.py core_dump
python3 make/make.py core_dump --target clean
```

- The target must be a plain name (`all`, `clean`, `core_dump`). Options (`-f`, `-C`, ...) and
  `VAR=value` overrides are refused, and `MAKEFLAGS` is ignored, so the model cannot point make at
  another Makefile or folder.
- **make runs the commands in the Makefile.** Only allow folders whose Makefiles you trust.
- A build that takes over 25 seconds is stopped (the agents give a tool 30 seconds).
- A failed build exits nonzero, so the agent sees it as a tool failure with the compiler's errors.
