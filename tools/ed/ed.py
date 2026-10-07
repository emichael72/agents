#!/usr/bin/env python3
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

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

# The file-system gate (context/paths.json) lives in agents/gatekeepers/fs
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gatekeepers" / "fs"))
import fs_gate  # noqa: E402

ACTIONS = ("replace", "lines", "insert", "write", "hex")
OPTIONS = ("action", "old", "new", "all", "start", "end", "line", "offset", "length")
MAX_BYTES = 2_000_000
CONTEXT = 3
HEX_LENGTH = 256  # Bytes hex shows by default
HEX_MAX = 4096  # The most bytes hex shows at once
HEX_ROW = 16


def target_file(path: str, action: str) -> tuple[Path, str]:
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
    target, shown = fs_gate.resolve(path, "output" if action == "write" else "file", "w")
    for protected, name in ((fs_gate.TOOLS_DIR, "tools"), (fs_gate.CONTEXT_DIR, "context"),
                            (fs_gate.GATEKEEPERS_DIR, "gatekeepers")):
        if target == protected or protected in target.parents:
            raise ValueError(f"'{shown}' is in the {name} folder, which ed does not change.")
    if ".git" in target.parts:
        raise ValueError(f"'{shown}' is inside a .git folder, which ed does not change.")
    if target.exists() and target.stat().st_size > MAX_BYTES:
        raise ValueError(f"'{shown}' is larger than {MAX_BYTES:,} bytes.")
    return target, shown


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


def as_lines(new: str, newline: str) -> list[str]:
    """
    Split text to insert into whole lines, each ending with the file's newline.
    Args:
        new: The text; "" means no lines.
        newline: "\\n" or "\\r\\n".
    Returns:
        list[str]: The lines.
    """
    if not new:
        return []
    return [line + newline for line in new.replace("\r\n", "\n").removesuffix("\n").split("\n")]


def snippet(text: str, first: int, last: int) -> str:
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
    start, end = max(1, first - CONTEXT), min(len(lines), max(first, last) + CONTEXT)
    width = len(str(end))
    return "\n".join(f"{n:>{width}}  {lines[n - 1]}" for n in range(start, end + 1))


def line_number(text: str, offset: int) -> int:
    """Return the 1-based line number of a character offset."""
    return text.count("\n", 0, offset) + 1


def hex_view(path: str, offset: Optional[int] = None, length: Optional[int] = None) -> str:
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
    target, shown = fs_gate.resolve(path, "file", "r")
    size = target.stat().st_size
    length = HEX_LENGTH if length is None else length
    if not 1 <= length <= HEX_MAX:
        raise ValueError(f"length must be from 1 to {HEX_MAX} bytes.")
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
    for at in range(0, len(data), HEX_ROW):
        chunk = data[at:at + HEX_ROW]
        hexes = " ".join(f"{b:02x}" for b in chunk)
        if len(chunk) > 8:
            hexes = hexes[:23] + " " + hexes[23:]  # A gap after the 8th byte, as hexdump -C
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        rows.append(f"{start + at:08x}  {hexes:<48}  |{text}|")
    if end + 1 < size:
        rows.append(f"({size - end - 1:,} more bytes; continue with offset {end + 1})")
    return "\n".join(rows)


def edit(path: str, action: str = "replace", old: Optional[str] = None, new: Optional[str] = None,
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
    if action not in ACTIONS:
        raise ValueError(f"Unknown action '{action}'. Use one of: {', '.join(ACTIONS)}.")
    if action == "hex":
        return hex_view(path, offset, length)  # Read only: nothing below runs
    target, shown = target_file(path, action)
    text = read_text(target, shown)
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
                offsets.append(line_number(text, at))
                at = text.find(old, at + 1)
            raise ValueError(f"old matches {count} places in '{shown}' (lines {', '.join(map(str, offsets))}); "
                             f"add surrounding text to make it unique, or set all to replace every one.")
        first = line_number(text, text.find(old))
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
            added = as_lines(new, newline)
            lines[start - 1:end] = added
            first, last = start, start + len(added) - 1
            summary = (f"replaced lines {start}-{end} with {len(added)} line(s)" if added
                       else f"deleted lines {start}-{end}")
        else:
            if line is None or not 0 <= line <= len(lines):
                raise ValueError(f"insert needs line from 0 (the top) to {len(lines)} (the end).")
            added = as_lines(new, newline)
            if not added:
                raise ValueError("insert needs new: the text to insert.")
            lines[line:line] = added
            first, last = line + 1, line + len(added)
            summary = f"inserted {len(added)} line(s) after line {line}"
        result = "".join(lines)

    if result == text and target.exists():
        return f"{shown}: no change (the new text is the same)"
    write_text(target, result)
    return f"{shown}: {summary}\n{snippet(result, first, last)}"


def parse(argv: list[str]) -> dict:
    """
    Read "<path> [--option value ...]". Values are taken verbatim, even when they start with "-" or
    span several lines, as the agents pass them.
    Args:
        argv: The arguments.
    Returns:
        dict: path and the given options.
    Raises:
        ValueError: If the path is missing or an option is unknown or has no value.
    """
    options: dict = {}
    rest = list(argv)
    while rest:
        argument = rest.pop(0)
        name = argument[2:] if argument.startswith("--") else None
        if name in OPTIONS:
            if not rest:
                raise ValueError(f"--{name} needs a value.")
            options[name] = rest.pop(0)
        elif "path" not in options:
            options["path"] = argument
        else:
            raise ValueError(f"Unexpected argument '{argument}'. Options: {', '.join('--' + o for o in OPTIONS)}.")
    if "path" not in options:
        raise ValueError("Give the file to edit, e.g. core_dump/src/main.c.")
    return options


def main(argv: Optional[list[str]] = None) -> str:
    """
    Parse the command line and apply the edit.
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        str: The edit's description.
    """
    o = parse(sys.argv[1:] if argv is None else argv)

    def number(name: str) -> Optional[int]:
        try:
            return int(o[name]) if name in o else None
        except ValueError:
            raise ValueError(f"{name} must be a whole number.") from None

    return edit(o["path"], o.get("action", "replace"), o.get("old"), o.get("new"),
                o.get("all", "").lower() in ("true", "1", "yes"), number("start"), number("end"), number("line"),
                number("offset"), number("length"))


if __name__ == "__main__":
    try:
        print(main())
    except (ValueError, OSError) as e:
        print(f"Error: {e}")
        sys.exit(1)
