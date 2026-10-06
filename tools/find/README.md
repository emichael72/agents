# Find

Finds files and folders by name with GNU find, under a folder inside the allowed folders
([`../../context/paths.json`](../../context/paths.json)), such as `core_dump`. Lists their paths,
sorted, as `<allowed name>/...`; folders end with `/`. At most 200. Read-only.

**Usage Example:**

```bash
python3 find/find.py core_dump --name "*.c"
python3 find/find.py core_dump --type d
python3 find/find.py core_dump --name makefile --ignore_case true --max_depth 1
```

| Option | Does |
| --- | --- |
| `name` | Glob for the name, e.g. `*.c` (`-name`, or `-iname` with `ignore_case`) |
| `type` | `f` for files, `d` for folders (`-type`) |
| `max_depth` | Folder levels to descend, 1 to 20 (`-maxdepth`) |
| `ignore_case` | Match the name regardless of case |

Only these tests are built, by the script; nothing from the model reaches find as an option, so
actions such as `-exec` or `-delete` cannot be asked for. The `.git`, `.venv`, `node_modules` and
`__pycache__` folders are skipped. To search file contents, use `grep`.
