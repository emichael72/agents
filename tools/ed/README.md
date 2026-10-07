# Ed

Edits a text file inside the allowed folders ([`../../context/paths.json`](../../context/paths.json)),
then shows the changed lines, numbered, with three lines of context so the edit can be checked. Its
`hex` action only reads: it shows part of any file, binary too, as a hex dump.

| Action              | Parameters            | Does                                                                                                                                                                                                       |
|---------------------|-----------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `replace` (default) | `old`, `new`, `all`   | Replaces the exact text `old` with `new`. `old` must match once; if it matches several places, the error lists their lines (add surrounding text, or set `all`).                                           |
| `lines`             | `start`, `end`, `new` | Replaces lines `start`..`end` (as numbered by `cat -n` in the shell) with `new`; an empty `new` deletes them.                                                                                              |
| `insert`            | `line`, `new`         | Inserts `new` after line `line`; `0` inserts at the top.                                                                                                                                                   |
| `write`             | `new`                 | Creates the file, or replaces all of it, with `new`.                                                                                                                                                       |
| `hex`               | `offset`, `length`    | Read only: shows `length` bytes (default 256, at most 4,096) from `offset` (default 0; negative counts from the end) as a hex dump, like `hexdump -C`, and says where to continue. Needs only read access. |

**Usage Example:**

```bash
python3 ed/ed.py core_dump/src/modules/pi.c --old "acos(-1.0)" --new "4.0 * atan(1.0)"
python3 ed/ed.py core_dump/src/main.c --action lines --start 12 --end 14 --new "..."
python3 ed/ed.py core_dump/src/modules/e.c --action write --new "$(cat e.c)"
python3 ed/ed.py core_dump/core_dump --action hex --offset 0 --length 64
```

`hex` output, a header with the range and the file's size, then 16 bytes a row:

```text
core_dump/core_dump: bytes 0-63 (0x0-0x3f) of 26,944
00000000  7f 45 4c 46 02 01 01 00  00 00 00 00 00 00 00 00  |.ELF............|
00000010  02 00 3e 00 01 00 00 00  30 06 40 00 00 00 00 00  |..>.....0.@.....|
00000020  40 00 00 00 00 00 00 00  40 5f 00 00 00 00 00 00  |@.......@_......|
00000030  00 00 00 00 40 00 38 00  0d 00 40 00 28 00 27 00  |....@.8...@.(.'.|
(26,880 more bytes; continue with offset 64)
```

- **Protected:** ed never changes the tools folder (the tools' code, including the gate) or the
  `context` folder (`paths.json`, models and instructions), so the model cannot widen its own
  access, nor anything in a `.git` folder (git's configuration can run programs).
- **Safe writes:** the new contents go to a temporary file that replaces the original in one step;
  permissions and line endings (LF or CRLF) are kept. A match that fails changes nothing.
- Edits only UTF-8 text files up to 2 MB. `write` ends the file with a newline. `hex` reads any
  file, of any size, in read-only folders too, but only the bytes it shows.

Together with `make` and `gcc`, ed lets the model write code and run it, within the allowed
folders. Allow only folders where that is acceptable.
