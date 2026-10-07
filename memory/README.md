# Memory

Notes the agents keep between runs: what they learned about a project, decisions made, and the
user's preferences. Folder `memory` in [`context/paths.json`](../context/paths.json), with read
and write access but no execute: the agents read and write it with the `shell` and `ed` tools,
and nothing stored here can run.

- `index.md` lists the notes, one line each; agents read it first, then only the notes they need.
- One topic per file (for example `core_dump.md`, `preferences.md`), kept short and up to date.
- Never secrets: no keys, passwords or tokens.

Everything here except this README is ignored by git: the notes are local to this machine. Delete
a note, or the whole folder's contents, to make the agents forget.
