# File editing

`ed` changes files in writable project folders and shows the edited lines afterward. Use it for editing; use [shell](../shell/README.md) for reading and running checks.

## An example

From the repository root:

~~~bash
python3 tools/ed/ed.py --old="acos(-1.0)" --new="4.0 * atan(1.0)" -- core_dump/src/modules/pi.c
~~~

The old text must match once. If it appears in several places, include more surrounding text or use `--all=true`. A failed match leaves the file alone.

Models often copy text with small differences: straight quotes for curly ones, a hyphen for a dash, different spacing.
When `old` is not found exactly, `ed` evens those out and applies the edit if that finds exactly one place, saying what
it evened out. If `old` is still not found, it shows the closest lines of the file, numbered, so the next try can copy
them, or replace them by number with `lines`. Text that differs in any other way is refused.

## Other edits

| Action | What to provide |
| --- | --- |
| replace (default) | `old` and `new` |
| lines | `start`, optional `end`, and `new`; line numbers start at 1 |
| insert | `line` and `new`; inserts after that line, or at the top for 0. Instead of `line`, `after` or `before` names the text of the line to insert next to, such as a heading; only one line may hold it |
| write | `new` containing the whole file; creates or overwrites it |
| hex | `offset` and `length` to inspect bytes without editing |

Empty replacement text deletes the selected text or lines.

Text edits require UTF-8 files no larger than 2,000,000 bytes. Replacements use a temporary file and preserve permissions and line endings; `write` adds a final newline. Hex inspection can read binary files and shows at most 4,096 bytes per call.

The tool refuses edits to tools, gatekeepers, shared context, and .git directories, even if a broad path permission would otherwise allow them. Other access comes from [context/paths.json](../../context/paths.json).
