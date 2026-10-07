#!/usr/bin/env python3
"""
Module: memory.py

Description:
    The agents' memory: short notes kept between runs, one topic per file in the folder named
    "memory" in context/paths.json (agents/.memory, read and write, no execute), with an index the
    agents load at start-up (.memory/index.md).

    Actions:
      - save: add a line to a topic's note (or, with replace, rewrite the note), and update the
        index line for the topic.
      - read: show a topic's note, or the index when no topic is given.
      - forget: delete a topic's note and its index line.

    Key design points:
      - Topics are plain names (letters, digits, - and _), stored as <topic>.md; nothing else in
        the folder is touched, and the folder is checked by the file-system gate.
      - Notes and the index stay small (MAX_NOTE characters per note); a note is for lasting facts,
        decisions and preferences, never secrets.
"""

import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional

# The file-system gate (context/paths.json) lives in agents/gatekeepers/fs
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gatekeepers" / "fs"))
import fs_gate  # noqa: E402

FOLDER = "memory"  # The allowed folder's name in context/paths.json
INDEX = "index.md"
TOPIC = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
MAX_NOTE = 4000
OPTIONS = ("action", "topic", "text", "summary", "replace")


def folder() -> Path:
    """
    The memory folder, which must allow reading and writing.
    Returns:
        Path: The folder.
    Raises:
        ValueError: If paths.json has no writable "memory" folder.
    """
    return fs_gate.resolve(FOLDER, "dir", "rw")[0]


def topic_name(topic: Optional[str]) -> str:
    """
    Turn the topic the model gave into a plain name: lowercase, and anything other than letters,
    digits, - and _ (spaces, slashes) becomes "-", so "core_dump preferences" is core_dump-preferences.
    Args:
        topic: The name the model gave.
    Returns:
        str: The plain name.
    Raises:
        ValueError: If nothing usable is left.
    """
    name = re.sub(r"[^a-z0-9_-]+", "-", (topic or "").strip().lower().removesuffix(".md")).strip("-")
    if not TOPIC.match(name) or name == "index":
        raise ValueError("Give a topic: a short name of letters, digits, - and _ (e.g. preferences, core_dump).")
    return name


def read_index(memory: Path) -> dict[str, str]:
    """Read the index: each topic and its one-line summary."""
    entries = {}
    index = memory / INDEX
    for line in index.read_text(encoding="utf-8").splitlines() if index.is_file() else []:
        match = re.match(r"^- \*\*([a-z0-9_-]+)\*\*: (.*)$", line)
        if match:
            entries[match.group(1)] = match.group(2)
    return entries


def write_index(memory: Path, entries: dict[str, str]) -> None:
    """Write the index, sorted by topic."""
    lines = ["# Memory index", "", "Topics saved by the agents; read one with the memory tool (action read).", ""]
    lines += [f"- **{topic}**: {summary}" for topic, summary in sorted(entries.items())]
    (memory / INDEX).write_text("\n".join(lines) + "\n", encoding="utf-8")


def save(topic: str, text: str, summary: Optional[str] = None, replace: bool = False) -> str:
    """
    Save to a topic's note and update its index line.
    Args:
        topic: The topic.
        text: The fact to add (one line), or the whole note with replace.
        summary: The topic's one-line index summary; None keeps it, or uses the first line.
        replace: Rewrite the note with text instead of adding a line.
    Returns:
        str: What was saved.
    Raises:
        ValueError: If the topic or text is missing, or the note would grow too large.
    """
    name, text = topic_name(topic), (text or "").strip()
    if not text:
        raise ValueError("Give the text to remember.")
    memory = folder()
    note = memory / f"{name}.md"
    if replace or not note.is_file():
        body = text if replace else f"- {text}"
    else:
        body = note.read_text(encoding="utf-8").rstrip("\n") + f"\n- {text}"
    if len(body) > MAX_NOTE:
        raise ValueError(f"The note would exceed {MAX_NOTE} characters; rewrite it shorter with replace.")
    note.write_text(body + "\n", encoding="utf-8")
    # The index line: the summary given, else the one there is, else the note's first line
    entries = read_index(memory)
    kept = re.sub(r" \(updated \d{4}-\d{2}-\d{2}\)$", "", entries.get(name, ""))
    first = text.splitlines()[0].lstrip("-# ").strip()
    entries[name] = f"{(summary or kept or first)[:120]} (updated {date.today()})"
    write_index(memory, entries)
    return f"Saved to memory/{name}.md" + (" (replaced)" if replace else "") + f":\n{body}"


def read(topic: Optional[str] = None) -> str:
    """
    Read a topic's note, or the index.
    Args:
        topic: The topic; None for the index.
    Returns:
        str: The note or the index.
    Raises:
        ValueError: If the topic does not exist.
    """
    memory = folder()
    if not topic:
        entries = read_index(memory)
        if not entries:
            return "The memory is empty."
        return "\n".join(f"- {name}: {summary}" for name, summary in sorted(entries.items()))
    note = memory / f"{topic_name(topic)}.md"
    if not note.is_file():
        raise ValueError(f"No note on '{topic}'. Topics: {', '.join(sorted(read_index(memory))) or 'none'}.")
    return note.read_text(encoding="utf-8").rstrip("\n")


def forget(topic: str) -> str:
    """
    Delete a topic's note and its index line.
    Args:
        topic: The topic.
    Returns:
        str: What was forgotten.
    Raises:
        ValueError: If the topic does not exist.
    """
    name, memory = topic_name(topic), folder()
    note = memory / f"{name}.md"
    entries = read_index(memory)
    if not note.is_file() and name not in entries:
        raise ValueError(f"No note on '{topic}'.")
    note.unlink(missing_ok=True)
    entries.pop(name, None)
    write_index(memory, entries)
    return f"Forgot '{name}'."


def main(argv: Optional[list[str]] = None) -> str:
    """
    Read "--action <save|read|forget> [--topic T] [--text X] [--summary S] [--replace true]" (values
    taken verbatim, as the agents pass them) and run it.
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        str: The result.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    options: dict = {}
    while argv:
        argument = argv.pop(0)
        if argument.startswith("--") and argument[2:] in OPTIONS and argv:
            options[argument[2:]] = argv.pop(0)
        else:
            raise ValueError(f"Unexpected argument '{argument}'.")
    action = options.get("action", "read")
    if action == "save":
        return save(options.get("topic", ""), options.get("text", ""), options.get("summary"),
                    options.get("replace", "").lower() in ("true", "1", "yes"))
    if action == "read":
        return read(options.get("topic"))
    if action == "forget":
        return forget(options.get("topic", ""))
    raise ValueError(f"Unknown action '{action}'; use save, read or forget.")


if __name__ == "__main__":
    try:
        print(main())
    except (ValueError, OSError) as e:
        print(f"Error: {e}")
        sys.exit(1)
