"""
Module: ed.py

Description:
    Edits text files for the agents, inside the allowed folders with write access
    (context/paths.json, checked by gatekeepers/fs/fs_gate.py), and shows any file as hex. Five actions:
      - replace: replace exact text (`old` with `new`); `old` must match once, unless `all` is set.
      - lines:   replace lines `start`..`end` (as `cat -n` in the shell numbers them) with `new`; an empty
                 `new` deletes them.
      - insert:  insert `new` after line `line` (0 inserts at the top).
      - write:   create the file, or replace all of it, with `new`.
      - hex:     read only: show `length` bytes from `offset` as a hex dump (offset, 16 bytes in hex,
                 then the printable characters), for looking into binary files.
    After an edit it shows the changed lines, numbered, with CONTEXT lines around them.

    Key design points:
      - Never edits the tools folder, the gatekeepers (the file-system and pull request gates) or
        the context folder (paths.json, models and instructions), so the model cannot widen its own
        access or weaken a gate, nor a
        .git folder (git's configuration can run programs).
      - Writes are atomic (a temporary file renamed over the original) and keep the file's
        permissions and line endings (LF or CRLF).
      - Only UTF-8 text files up to MAX_BYTES; a failed match changes nothing.
      - hex needs only read access and reads just the bytes it shows (at most HEX_MAX), so it
        works on any readable file, of any size.
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Import the shared filesystem gate from the repository root (the nearest folder above
# holding pyproject.toml).
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents
                            if (p / "pyproject.toml").is_file())))
from gatekeepers import CONTEXT_DIR, GATEKEEPERS_DIR, TOOLS_DIR
from gatekeepers.fs.fs_gate import FsGate
from tools.common.cli import ToolArgumentParser


class Editor:
    """
    Edits and shows files in the allowed folders, as the file-system gate permits.
    """

    VERSION = "1.0.0"
    ACTIONS = ("replace", "lines", "insert", "write", "hex")
    MAX_BYTES = 2_000_000
    CONTEXT = 3  # Lines shown around a change
    HEX_LENGTH = 256  # Bytes hex shows by default
    HEX_MAX = 4096  # The most bytes hex shows at once
    HEX_ROW = 16
    PROTECTED = ((TOOLS_DIR, "tools"), (CONTEXT_DIR, "context"), (GATEKEEPERS_DIR, "gatekeepers"))

    def __init__(self, gate: Optional[FsGate] = None) -> None:
        """
        Args:
            gate: The allowed folders; None reads context/paths.json.
        """
        self.gate = gate or FsGate.load()

    def target_file(self, path: str, action: str) -> tuple[Path, str]:
        """
        Resolve the file to edit and refuse protected places.
        Args:
            path: <allowed name>/<file>.
            action: The edit action; only "write" may create a file.
        Returns:
            tuple[Path, str]: The absolute path, and the path as shown.
        Raises:
            ValueError: If the path is not allowed, protected, missing, binary or too large.
        """
        target, shown = self.gate.resolve(path, "output" if action == "write" else "file", "w")
        for protected, name in self.PROTECTED:
            if target == protected or protected in target.parents:
                raise ValueError(f"'{shown}' is in the {name} folder, which ed does not change.")
        if ".git" in target.parts:
            raise ValueError(f"'{shown}' is inside a .git folder, which ed does not change.")
        if target.exists() and target.stat().st_size > self.MAX_BYTES:
            raise ValueError(f"'{shown}' is larger than {self.MAX_BYTES:,} bytes.")
        return target, shown

    @staticmethod
    def read_text(target: Path, shown: str) -> str:
        """
        Read a UTF-8 text file without translating its line endings.
        Args:
            target: The file.
            shown: Its path as shown, for errors.
        Returns:
            str: The text ("" for a file that does not exist yet).
        Raises:
            ValueError: If the file is binary or not UTF-8.
        """
        if not target.exists():
            return ""
        data = target.read_bytes()
        if b"\0" in data[:8192]:
            raise ValueError(f"'{shown}' is a binary file.")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"'{shown}' is not UTF-8 text.") from None

    @staticmethod
    def write_text(target: Path, text: str) -> None:
        """
        Replace a file's contents atomically, keeping its permissions.
        Args:
            target: The file (created if missing).
            text: The new contents.
        """
        mode = target.stat().st_mode & 0o7777 if target.exists() else None
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".ed")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @staticmethod
    def as_lines(new: str, newline: str) -> list[str]:
        """
        Split text to insert into whole lines, each ending with the file's newline.
        Args:
            new: The text; an empty string means no lines.
            newline: The file's line ending, either LF or CRLF.
        Returns:
            list[str]: The lines.
        """
        if not new:
            return []
        return [line + newline for line in new.replace("\r\n", "\n").removesuffix("\n").split("\n")]

    @classmethod
    def snippet(cls, text: str, first: int, last: int) -> str:
        """
        Show lines first..last (1-based) with CONTEXT lines around them, numbered.
        Args:
            text: The file's new text.
            first: The first changed line.
            last: The last changed line (less than first when lines were only removed).
        Returns:
            str: The numbered lines.
        """
        lines = text.splitlines()
        if not lines:
            return "(the file is now empty)"
        start, end = max(1, first - cls.CONTEXT), min(len(lines), max(first, last) + cls.CONTEXT)
        width = len(str(end))
        return "\n".join(f"{n:>{width}}  {lines[n - 1]}" for n in range(start, end + 1))

    @staticmethod
    def line_number(text: str, offset: int) -> int:
        """Return the 1-based line number of a character offset."""
        return text.count("\n", 0, offset) + 1

    def hex_view(self, path: str, offset: Optional[int] = None, length: Optional[int] = None) -> str:
        """
        Show part of a file as a hex dump, like hexdump -C: each row is the offset, 16 bytes in hex
        and the same bytes as text (printable ASCII, "." for the rest).
        Args:
            path: <allowed name>/<file>; read access is enough.
            offset: The first byte (0-based, default 0); negative counts from the end (-16 = the last 16 bytes).
            length: How many bytes (default HEX_LENGTH, at most HEX_MAX).
        Returns:
            str: A header with the range shown and the file's size, then the rows.
        Raises:
            ValueError: If the path is refused, or offset or length is out of range.
        """
        target, shown = self.gate.resolve(path, "file", "r")
        size = target.stat().st_size
        length = self.HEX_LENGTH if length is None else length
        if not 1 <= length <= self.HEX_MAX:
            raise ValueError(f"length must be from 1 to {self.HEX_MAX} bytes.")
        start = offset or 0
        if start < 0:
            start = max(0, size + start)
        if start >= size and size:
            raise ValueError(f"offset {offset} is past the end of '{shown}' ({size:,} bytes).")
        with target.open("rb") as f:
            f.seek(start)
            data = f.read(length)
        if not data:
            return f"{shown}: empty file"
        end = start + len(data) - 1
        rows = [f"{shown}: bytes {start:,}-{end:,} (0x{start:x}-0x{end:x}) of {size:,}"]
        for at in range(0, len(data), self.HEX_ROW):
            chunk = data[at:at + self.HEX_ROW]
            hexes = " ".join(f"{b:02x}" for b in chunk)
            if len(chunk) > 8:
                hexes = hexes[:23] + " " + hexes[23:]  # A gap after the 8th byte, as hexdump -C
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            rows.append(f"{start + at:08x}  {hexes:<48}  |{text}|")
        if end + 1 < size:
            rows.append(f"({size - end - 1:,} more bytes; continue with offset {end + 1})")
        return "\n".join(rows)

    def edit(self, path: str, action: str = "replace", old: Optional[str] = None, new: Optional[str] = None,
                 replace_all: bool = False, start: Optional[int] = None, end: Optional[int] = None,
                 line: Optional[int] = None, offset: Optional[int] = None, length: Optional[int] = None) -> str:
        """
        Apply one edit and describe it, or with hex, show part of a file.
        Args:
            path: <allowed name>/<file>.
            action: "replace", "lines", "insert", "write" or "hex".
            old: replace: the exact text to find.
            new: The new text (replace, lines, insert, write).
            replace_all: replace: change every occurrence of old.
            start: lines: the first line to replace.
            end: lines: the last line to replace (default: start).
            line: insert: the line to insert after (0 for the top).
            offset: hex: the first byte; negative counts from the end.
            length: hex: how many bytes.
        Returns:
            str: What changed, then the changed lines with context.
        Raises:
            ValueError: If the request is incomplete, does not match the file, or the path is refused.
        """
        if action not in self.ACTIONS:
            raise ValueError(f"Unknown action '{action}'. Use one of: {', '.join(self.ACTIONS)}.")
        if action == "hex":
            return self.hex_view(path, offset, length)  # Read only: nothing below runs
        target, shown = self.target_file(path, action)
        text = self.read_text(target, shown)
        newline = "\r\n" if "\r\n" in text else "\n"
        new = new or ""

        if action == "write":
            if new and not new.endswith("\n"):
                new += "\n"  # Text files end with a newline
            result, summary = new, f"wrote {len(new.splitlines())} lines"
            first, last = 1, len(new.splitlines())
        elif action == "replace":
            if not old:
                raise ValueError("replace needs old: the exact text to replace.")
            count = text.count(old)
            if count == 0:
                raise ValueError(f"old was not found in '{shown}'; it must match exactly, including spaces and "
                                 f"indentation. Show the file with cat -n in the shell and copy the text from it.")
            if count > 1 and not replace_all:
                offsets, at = [], text.find(old)
                while at != -1:
                    offsets.append(self.line_number(text, at))
                    at = text.find(old, at + 1)
                raise ValueError(f"old matches {count} places in '{shown}' (lines {', '.join(map(str, offsets))}); "
                                 f"add surrounding text to make it unique, or set all to replace every one.")
            first = self.line_number(text, text.find(old))
            result = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            last = first + new.count("\n")
            summary = f"replaced {count if replace_all else 1} occurrence(s)"
        else:
            lines = text.splitlines(keepends=True)
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += newline  # So inserted lines start on a line of their own
            if action == "lines":
                end = start if end is None else end
                if start is None or not 1 <= start <= end <= len(lines):
                    raise ValueError(f"lines needs 1 <= start <= end <= {len(lines)} (the file's line count).")
                added = self.as_lines(new, newline)
                lines[start - 1:end] = added
                first, last = start, start + len(added) - 1
                summary = (f"replaced lines {start}-{end} with {len(added)} line(s)" if added
                           else f"deleted lines {start}-{end}")
            else:
                if line is None or not 0 <= line <= len(lines):
                    raise ValueError(f"insert needs line from 0 (the top) to {len(lines)} (the end).")
                added = self.as_lines(new, newline)
                if not added:
                    raise ValueError("insert needs new: the text to insert.")
                lines[line:line] = added
                first, last = line + 1, line + len(added)
                summary = f"inserted {len(added)} line(s) after line {line}"
            result = "".join(lines)

        if result == text and target.exists():
            return f"{shown}: no change (the new text is the same)"
        self.write_text(target, result)
        return f"{shown}: {summary}\n{self.snippet(result, first, last)}"

    @classmethod
    def build_parser(cls) -> ToolArgumentParser:
        """
        The command line: "[--action=...] [--old=...] [--new=...] ... -- <path>".
        Returns:
            ToolArgumentParser: The parser.
        """
        parser = ToolArgumentParser("ed", cls.VERSION, "Edit a text file in the allowed folders, or show any file as hex.")
        parser.add_argument("path", nargs="?", help="<allowed name>/<file>, e.g. core_dump/src/main.c")
        parser.add_argument("--action", default="replace", help=f"One of: {', '.join(cls.ACTIONS)} (default replace)")
        parser.add_argument("--old", help="replace: the exact text to replace")
        parser.add_argument("--new", help="The new text: the replacement, the lines to put in, or the whole file")
        parser.add_argument("--all", type=ToolArgumentParser.boolean, default=False,
                            help="replace: change every occurrence of old (true or false)")
        for name, about in (("start", "lines: the first line to replace"), ("end", "lines: the last line (default start)"),
                            ("line", "insert: the line to insert after; 0 for the top"),
                            ("offset", "hex: the first byte; negative counts from the end"),
                            ("length", f"hex: how many bytes (default {cls.HEX_LENGTH})")):
            parser.add_argument(f"--{name}", type=int, help=about)
        return parser

    def run(self, args: argparse.Namespace) -> str:
        """
        Apply the edit a command line asks for.
        Args:
            args: The command line, from `build_parser`.
        Returns:
            str: The edit's description.
        Raises:
            ValueError: If the path is missing, or the edit is refused.
        """
        if not args.path:
            raise ValueError("Give the file to edit, e.g. core_dump/src/main.c.")
        return self.edit(args.path, args.action, args.old, args.new, args.all, args.start, args.end, args.line,
                         args.offset, args.length)


def main(argv: Optional[list[str]] = None) -> int:
    """
    Parse the command line, apply the edit and print what changed, or "Error: <reason>".
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        int: 0 on success, 1 on an error.
    """
    try:
        args = Editor.build_parser().parse_args(argv)
        print(Editor().run(args))
    except (ValueError, OSError) as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
