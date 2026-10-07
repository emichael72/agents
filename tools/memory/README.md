# Memory

The agents' memory between runs: short notes, one topic per file, in the folder named `memory` in
[`context/paths.json`](../../context/paths.json) (`agents/.memory/`, read and write, no execute).
Every agent loads `.memory/index.md` into its instructions at start-up, so it knows what it
remembers without being asked; this tool saves, reads and forgets the notes behind the index. See
"Memory" in the [repository README](../../README.md#memory).

| Action   | Parameters                                       | Does                                                                                                |
|----------|--------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| `save`   | `topic`, `text`, `summary`, `replace` (optional) | Adds `text` as a line to the topic's note (`replace` rewrites the note), and updates its index line |
| `read`   | `topic` (optional)                               | The topic's note, or the index                                                                      |
| `forget` | `topic`                                          | Deletes the note and its index line                                                                 |

**Usage Example:**

```bash
python3 memory/memory.py --action=save --topic=preferences --text="Prefers short answers"
python3 memory/memory.py --action=read --topic=preferences
python3 memory/memory.py --action=forget --topic=preferences
```

Topics are plain names (letters, digits, `-`, `_`), stored as `<topic>.md`; a note holds at most
4,000 characters. The notes are local to this machine (git ignores `.memory/`). Never store secrets.
