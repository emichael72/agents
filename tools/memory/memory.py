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

# Import the shared filesystem gate from the repository root (the nearest folder above
# holding pyproject.toml).
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents
                            if (p / "pyproject.toml").is_file())))
from gatekeepers.fs.fs_gate import FsGate


class Memory:
    """
    The notes in the memory folder, and their index.
    """

    VERSION = "1.0.0"
    FOLDER = "memory"  # The allowed folder's name in context/paths.json
    INDEX = "index.md"
    TOPIC = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
    MAX_NOTE = 4000
    OPTIONS = ("action", "topic", "text", "summary", "replace")

    def __init__(self, gate: Optional[FsGate] = None) -> None:
        """
        Args:
            gate: The allowed folders; None reads context/paths.json.
        """
        self.gate = gate or FsGate.load()

    def folder(self) -> Path:
        """
        The memory folder, which must allow reading and writing. It is created on first use (it is
        ignored by git, so a fresh checkout has none), but only as the folder paths.json names.
        Returns:
            Path: The folder.
        Raises:
            ValueError: If paths.json has no writable "memory" folder.
        """
        entry = self.gate.folders.get(self.FOLDER)
        if entry is not None and "w" in entry.access and not entry.path.exists():
            entry.path.mkdir(parents=True)
        return self.gate.resolve(self.FOLDER, "dir", "rw")[0]

    @classmethod
    def topic_name(cls, topic: Optional[str]) -> str:
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
        if not cls.TOPIC.match(name) or name == "index":
            raise ValueError("Give a topic: a short name of letters, digits, - and _ (e.g. preferences, core_dump).")
        return name

    @classmethod
    def read_index(cls, memory: Path) -> dict[str, str]:
        """Read the index: each topic and its one-line summary."""
        entries = {}
        index = memory / cls.INDEX
        for line in index.read_text(encoding="utf-8").splitlines() if index.is_file() else []:
            match = re.match(r"^- \*\*([a-z0-9_-]+)\*\*: (.*)$", line)
            if match:
                entries[match.group(1)] = match.group(2)
        return entries

    @classmethod
    def write_index(cls, memory: Path, entries: dict[str, str]) -> None:
        """Write the index, sorted by topic."""
        lines = ["# Memory index", "", "Topics saved by the agents; read one with the memory tool (action read).", ""]
        lines += [f"- **{topic}**: {summary}" for topic, summary in sorted(entries.items())]
        (memory / cls.INDEX).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def save(self, topic: str, text: str, summary: Optional[str] = None, replace: bool = False) -> str:
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
        name, text = self.topic_name(topic), (text or "").strip()
        if not text:
            raise ValueError("Give the text to remember.")
        memory = self.folder()
        note = memory / f"{name}.md"
        if replace or not note.is_file():
            body = text if replace else f"- {text}"
        else:
            body = note.read_text(encoding="utf-8").rstrip("\n") + f"\n- {text}"
        if len(body) > self.MAX_NOTE:
            raise ValueError(f"The note would exceed {self.MAX_NOTE} characters; rewrite it shorter with replace.")
        note.write_text(body + "\n", encoding="utf-8")
        # The index line: the summary given, else the one there is, else the note's first line
        entries = self.read_index(memory)
        kept = re.sub(r" \(updated \d{4}-\d{2}-\d{2}\)$", "", entries.get(name, ""))
        first = text.splitlines()[0].lstrip("-# ").strip()
        entries[name] = f"{(summary or kept or first)[:120]} (updated {date.today()})"
        self.write_index(memory, entries)
        return f"Saved to memory/{name}.md" + (" (replaced)" if replace else "") + f":\n{body}"

    def read(self, topic: Optional[str] = None) -> str:
        """
        Read a topic's note, or the index.
        Args:
            topic: The topic; None for the index.
        Returns:
            str: The note or the index.
        Raises:
            ValueError: If the topic does not exist.
        """
        memory = self.folder()
        if not topic:
            entries = self.read_index(memory)
            if not entries:
                return "The memory is empty."
            return "\n".join(f"- {name}: {summary}" for name, summary in sorted(entries.items()))
        note = memory / f"{self.topic_name(topic)}.md"
        if not note.is_file():
            raise ValueError(f"No note on '{topic}'. Topics: {', '.join(sorted(self.read_index(memory))) or 'none'}.")
        return note.read_text(encoding="utf-8").rstrip("\n")

    def forget(self, topic: str) -> str:
        """
        Delete a topic's note and its index line.
        Args:
            topic: The topic.
        Returns:
            str: What was forgotten.
        Raises:
            ValueError: If the topic does not exist.
        """
        name, memory = self.topic_name(topic), self.folder()
        note = memory / f"{name}.md"
        entries = self.read_index(memory)
        if not note.is_file() and name not in entries:
            raise ValueError(f"No note on '{topic}'.")
        note.unlink(missing_ok=True)
        entries.pop(name, None)
        self.write_index(memory, entries)
        return f"Forgot '{name}'."

    @classmethod
    def parse(cls, argv: list[str]) -> dict:
        """
        Read "--action <save|read|forget> [--topic T] [--text X] [--summary S] [--replace true]". Values
        are taken verbatim, as the agents pass them.
        Args:
            argv: The arguments.
        Returns:
            dict: The given options.
        Raises:
            ValueError: If an argument is not a known option with a value.
        """
        rest = list(argv)
        options: dict = {}
        while rest:
            argument = rest.pop(0)
            if argument.startswith("--") and argument[2:] in cls.OPTIONS and rest:
                options[argument[2:]] = rest.pop(0)
            else:
                raise ValueError(f"Unexpected argument '{argument}'.")
        return options

    def run(self, argv: list[str]) -> str:
        """
        Run the action a command line asks for.
        Args:
            argv: The arguments, as `parse` reads them.
        Returns:
            str: The result.
        Raises:
            ValueError: If the arguments are invalid, or the action fails.
        """
        options = self.parse(argv)
        action = options.get("action", "read")
        if action == "save":
            return self.save(options.get("topic", ""), options.get("text", ""), options.get("summary"),
                             options.get("replace", "").lower() in ("true", "1", "yes"))
        if action == "read":
            return self.read(options.get("topic"))
        if action == "forget":
            return self.forget(options.get("topic", ""))
        raise ValueError(f"Unknown action '{action}'; use save, read or forget.")


def main(argv: Optional[list[str]] = None) -> int:
    """
    Parse the command line, run the action and print its result, or "Error: <reason>".
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        int: 0 on success, 1 on an error.
    """
    argv = sys.argv[1:] if argv is None else argv
    if argv in (["-v"], ["--version"]):
        print(f"memory {Memory.VERSION}")
        return 0
    try:
        print(Memory().run(argv))
    except (ValueError, OSError) as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
