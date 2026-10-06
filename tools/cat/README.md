# View File

Shows a text file with line numbers, up to 200 lines per call. The first line says which lines are
shown and how many the file has, so the next range can be asked for with `start`. Read-only.

Only files inside the folders named in [`../allowed_paths.json`](../allowed_paths.json) can be
read; a path starts with one of those names, e.g. `core_dump/src/main.c`. Binary files and files
over 2 MB are refused.

**Usage Example:**

```bash
python3 cat/cat.py core_dump/src/modules/pi.c
python3 cat/cat.py core_dump/src/main.c --start 40 --count 20
```
