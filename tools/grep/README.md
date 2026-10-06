# Grep

Searches file contents with GNU grep, recursively, in a file or folder inside the allowed folders
([`../../context/paths.json`](../../context/paths.json)), such as `core_dump`. Matches are shown as
`file:line:text` with paths as `<allowed name>/...`, at most 50. Read-only.

**Usage Example:**

```bash
python3 grep/grep.py print_pi --path core_dump
python3 grep/grep.py "acos" --path core_dump --include "*.c" --context 2
python3 grep/grep.py "TODO" --path core_dump --ignore_case true --files_only true
```

| Option | Does |
| --- | --- |
| `path` | File or folder to search (default: `tools`) |
| `ignore_case` | Match regardless of case (`-i`) |
| `fixed` | Plain text, not a regular expression (`-F`); the default is extended regular expressions (`-E`) |
| `files_only` | List the matching files (`-l`) |
| `context` | Lines around each match, 0 to 5 (`-C`) |
| `include` | Only files whose names match a glob, e.g. `*.c` (`--include`) |

The pattern is passed with `-e` and the path after `--`, so neither can act as a grep option; every
other option comes from the typed parameters above. Binary files and the `.git`, `.venv`,
`node_modules` and `__pycache__` folders are skipped. To search file names, use `find`.
