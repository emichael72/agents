#!/usr/bin/env python3
"""
Module: fs_gate.py

Description:
    The file-system gate: the one rule for which files and folders the tools may touch, and how.
    The allowed folders and their access rights are listed in context/paths.json. This folder holds
    no tool.json, so the agents do not offer it as a tool.

    A path the model gives starts with the name of an allowed folder: `core_dump/src/pi.c` means
    `src/pi.c` inside the folder that paths.json names core_dump. Paths are resolved (`..` and
    symbolic links included) before they are checked, so nothing outside the allowed folders can
    be reached.

    Access rights, per folder and inherited by everything inside it (a "subpaths" entry overrides
    them for one sub-folder):
      - r: read; w: write (create, change, delete); x: execute (run programs found there, or make,
        which runs the folder's Makefile).
      - An entry given as a plain string is read-only.

    It lives in agents/gatekeepers/fs. Python tools import it; Bash tools run it:
        python3 ../gatekeepers/fs/fs_gate.py PATH [--dir | --file] [--need r|w|x]
    which prints "<absolute path><TAB><path as shown>" or "Error: ..." with exit status 1.

    FS_GATE_PATHS may name another JSON file of the same shape, for a program that runs a tool on
    its own files (pr_gate runs doxy on a downloaded pull request). The agents only pass the fixed
    "env" of a tool's manifest, so the model cannot set it.
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# The repository root (the nearest folder above holding pyproject.toml); relative allowed
# folders start here
REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
TOOLS_DIR = REPO_ROOT / "tools"
GATEKEEPERS_DIR = REPO_ROOT / "gatekeepers"
CONTEXT_DIR = REPO_ROOT / "context"
PATHS_FILE = CONTEXT_DIR / "paths.json"
RIGHTS = {"r": "read", "w": "write", "x": "execute"}


@dataclass
class Folder:
    """An allowed folder: its name, location, access rights and per-sub-folder overrides."""
    name: str
    path: Path
    access: str = "r"
    subpaths: dict[Path, str] = field(default_factory=dict)  # Absolute sub-folder -> access

    def access_at(self, target: Path) -> str:
        """
        The access that applies to a file or folder inside this folder: that of the deepest
        sub-folder override containing it, else the folder's own.
        Args:
            target: An absolute path inside the folder.
        Returns:
            str: Some of "rwx".
        """
        matches = [p for p in self.subpaths if p == target or p in target.parents]
        return self.subpaths[max(matches, key=lambda p: len(p.parts))] if matches else self.access


def parse_access(value: str, where: str) -> str:
    """
    Check an access string such as "rwx" or "r".
    Args:
        value: The string from paths.json.
        where: Which entry it belongs to, for the error.
    Returns:
        str: The rights, in rwx order.
    Raises:
        ValueError: If it holds anything but r, w and x.
    """
    if not set(value) <= set(RIGHTS):
        raise ValueError(f"Access '{value}' for {where} must use only r, w and x.")
    return "".join(r for r in RIGHTS if r in value)


def load_allowed(path: Optional[Path] = None) -> dict[str, Folder]:
    """
    Read the allowed folders. Each entry is a path string (read-only), or
    {"path": ..., "access": "rwx", "subpaths": {"build": "rw", ...}}.
    Args:
        path: The JSON file; None uses FS_GATE_PATHS, then context/paths.json.
    Returns:
        dict[str, Folder]: The folders, by name.
    """
    path = path or Path(os.environ.get("FS_GATE_PATHS") or PATHS_FILE)
    folders = {}
    for name, entry in json.loads(path.read_text(encoding="utf-8"))["paths"].items():
        if isinstance(entry, str):
            base = (REPO_ROOT / Path(entry).expanduser()).resolve()
            folders[name] = Folder(name, base)
            continue
        base = (REPO_ROOT / Path(entry["path"]).expanduser()).resolve()
        subpaths = {(base / sub).resolve(): parse_access(access, f"{name}/{sub}")
                    for sub, access in entry.get("subpaths", {}).items()}
        folders[name] = Folder(name, base, parse_access(entry.get("access", "r"), name), subpaths)
    return folders


def locate(target: Path, allowed: dict[str, Folder]) -> Optional[tuple[Folder, str]]:
    """
    Find the allowed folder an absolute path is in.
    Args:
        target: A resolved absolute path.
        allowed: The allowed folders.
    Returns:
        Optional[tuple[Folder, str]]: The folder and the path as shown (<name>/...), or None.
    """
    for folder in sorted(allowed.values(), key=lambda f: -len(f.path.parts)):  # Deepest first
        if target == folder.path or folder.path in target.parents:
            return folder, folder.name if target == folder.path else f"{folder.name}/{target.relative_to(folder.path)}"
    return None


def resolve(path: str, kind: str = "any", need: str = "r",
            allowed: Optional[dict[str, Folder]] = None) -> tuple[Path, str]:
    """
    Turn a "<name>/<sub/path>" into a file or folder inside the allowed folder of that name, with
    the access needed.
    Args:
        path: The path the model gave.
        kind: "dir" or "file" to require one, "any" for either, or "output" for a file that may not
            exist yet (its folder must exist, inside the allowed folder).
        need: The rights required there, e.g. "r", "w" or "x".
        allowed: The allowed folders; None reads them.
    Returns:
        tuple[Path, str]: The absolute path, and the path as it should be shown ("<name>/<sub/path>").
    Raises:
        ValueError: If the name is not allowed, the path leaves its folder, lacks the access, does
            not exist, or is not of the required kind.
    """
    allowed = load_allowed() if allowed is None else allowed
    name, _, rest = path.strip().strip("/").partition("/")
    if name not in allowed:
        raise ValueError(f"'{name}' is not an allowed folder. Allowed: {', '.join(sorted(allowed))} "
                         f"(paths start with one of these names, e.g. {sorted(allowed)[0]}/...).")
    folder = allowed[name]
    target = (folder.path / rest).resolve()
    if target != folder.path and folder.path not in target.parents:
        raise ValueError(f"'{path}' is outside the allowed folder '{name}'.")
    access = folder.access_at(target)
    missing = [RIGHTS[r] for r in need if r not in access]
    if missing:
        raise ValueError(f"'{path}' does not allow {' and '.join(missing)} (its access is '{access or 'none'}').")
    if kind == "output":
        if target.is_dir() or not target.parent.is_dir():
            raise ValueError(f"'{path}' must be a file name in an existing folder.")
    elif not target.exists():
        raise ValueError(f"'{path}' does not exist.")
    if kind == "dir" and not target.is_dir():
        raise ValueError(f"'{path}' is not a folder.")
    if kind == "file" and not target.is_file():
        raise ValueError(f"'{path}' is not a file.")
    display_path = name if target == folder.path else f"{name}/{target.relative_to(folder.path)}"
    return target, display_path


def display(text: str, allowed: Optional[dict[str, Folder]] = None) -> str:
    """
    Show the absolute paths in a command's output the way the model gives them (<name>/...).
    Args:
        text: The output, e.g. a compiler's messages.
        allowed: The allowed folders; None reads them.
    Returns:
        str: The text, with each allowed folder's absolute path replaced by its name.
    """
    allowed = load_allowed() if allowed is None else allowed
    for folder in sorted(allowed.values(), key=lambda f: -len(str(f.path))):  # Deepest first
        text = text.replace(str(folder.path), folder.name)
    return text


def describe(allowed: Optional[dict[str, Folder]] = None) -> str:
    """
    List the allowed folders and their access, for a tool called without a path.
    Args:
        allowed: The allowed folders; None reads them.
    Returns:
        str: One line per folder (and sub-folder override).
    """
    allowed = load_allowed() if allowed is None else allowed
    width = max(map(len, allowed))
    lines = ["Allowed folders (paths start with one of these names; access r=read w=write x=execute):"]
    for name, folder in sorted(allowed.items()):
        lines.append(f"{name:<{width}}  {folder.access or '-':<3}  {folder.path}")
        for sub, access in sorted(folder.subpaths.items()):
            lines.append(f"{'':<{width}}  {access or '-':<3}    {name}/{sub.relative_to(folder.path)}")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check a tool path against context/paths.json.")
    parser.add_argument("path", help="<allowed name>/<sub/path>")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dir", action="store_const", const="dir", dest="kind", help="Require a folder")
    group.add_argument("--file", action="store_const", const="file", dest="kind", help="Require a file")
    parser.add_argument("--need", default="r", help="Rights required: some of r, w, x (default r)")
    args = parser.parse_args()
    try:
        absolute, shown = resolve(args.path, args.kind or "any", args.need)
    except (ValueError, OSError) as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"{absolute}\t{shown}")
