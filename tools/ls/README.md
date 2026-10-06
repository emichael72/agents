# List Files

Lists a folder: folders first, then files, with sizes and modification dates. Output stops after
200 entries. Read-only.

Only the folders named in [`allowed_paths.json`](allowed_paths.json), and the folders inside
them, can be listed:

```json
"paths": {
  "core_dump": "~/projects/core_dump",
  "tools": "."
}
```

A path starts with one of those names: `core_dump` lists the project, `core_dump/src/modules` a
folder inside it. Without a path, the tool lists the allowed names and where they point. Folder
values are absolute, start with `~`, or are relative to the tools folder. To allow another
folder, add a name to the file; the tool reads it on every call, so no restart is needed.

Paths are resolved, `..` and symbolic links included, before they are checked, so a path that
leads outside its allowed folder is refused.

**Usage Example:**

```bash
python3 ls/ls.py                        # the allowed folders
python3 ls/ls.py core_dump/src/modules
```
