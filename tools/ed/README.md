# Ed

Edits a text file inside the allowed folders ([`../allowed_paths.json`](../allowed_paths.json)),
then shows the changed lines, numbered, with three lines of context so the edit can be checked.

| Action | Parameters | Does |
| --- | --- | --- |
| `replace` (default) | `old`, `new`, `all` | Replaces the exact text `old` with `new`. `old` must match once; if it matches several places, the error lists their lines (add surrounding text, or set `all`). |
| `lines` | `start`, `end`, `new` | Replaces lines `start`..`end` (as numbered by `cat`) with `new`; an empty `new` deletes them. |
| `insert` | `line`, `new` | Inserts `new` after line `line`; `0` inserts at the top. |
| `write` | `new` | Creates the file, or replaces all of it, with `new`. |

**Usage Example:**

```bash
python3 ed/ed.py core_dump/src/modules/pi.c --old "acos(-1.0)" --new "4.0 * atan(1.0)"
python3 ed/ed.py core_dump/src/main.c --action lines --start 12 --end 14 --new "..."
python3 ed/ed.py core_dump/src/modules/e.c --action write --new "$(cat e.c)"
```

- **Protected:** ed never changes the tools folder (the tools' code and `allowed_paths.json`, so
  the model cannot widen its own access) or anything in a `.git` folder (git's configuration can
  run programs).
- **Safe writes:** the new contents go to a temporary file that replaces the original in one step;
  permissions and line endings (LF or CRLF) are kept. A match that fails changes nothing.
- Only UTF-8 text files up to 2 MB. `write` ends the file with a newline.

Together with `make` and `gcc`, ed lets the model write code and run it, within the allowed
folders. Allow only folders where that is acceptable.
