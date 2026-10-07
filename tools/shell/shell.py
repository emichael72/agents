#!/usr/bin/env python3
"""
Module: shell.py

Description:
    A shell for the agents: runs a command line made of allowed commands (commands.json) in one of
    the allowed folders (context/paths.json), inside a bubblewrap sandbox.

    The sandbox is the boundary. It sees /usr (read-only), a private /tmp, and each allowed folder
    at /work/<name>: read-only, or writable with "w" access (sub-folder overrides apply). Nothing
    else exists in it, and it has no network. So a command can only touch what paths.json allows,
    whatever its arguments.

    The command line is checked before it runs:
      - Every command (after |, ||, && or ;) must be in commands.json, or be a program inside a
        folder with "x" access (./core_dump). A command that is also its own tool is refused: the
        dedicated tool wins.
      - No redirection (< >), background (&), subshells or groups, $(...) or backticks, or
        line breaks. "2>&1" and "2>/dev/null" are dropped: errors already appear in the output.
      - Commands marked "needs": "x" (make) need execute access where they run; cd is followed to
        know where that is.
    The check is stricter than bash, never looser: it may refuse an unusual line, but it cannot
    pass a line in which bash would find a command it did not see.

    Also: git runs with hooks and fsmonitor off, and .git/config and .git/hooks stay read-only in
    writable folders, so nothing planted in a repository runs later outside the sandbox. Output
    shows /work/<name> as <name>; it stops after TIMEOUT seconds and MAX_LINES lines.
"""

import grp
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Optional

# Import the shared filesystem gate from the repository root (the nearest folder above
# holding pyproject.toml).
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents
                            if (p / "pyproject.toml").is_file())))
from gatekeepers.fs import fs_gate

COMMANDS_FILE = Path(__file__).resolve().parent / "commands.json"
CLANG_FORMAT = fs_gate.CONTEXT_DIR / "clang-format.yaml"  # The default C/C++ style, at /work/.clang-format
CLANG_TIDY = fs_gate.CONTEXT_DIR / "clang-tidy.yaml"  # The default C/C++ checks, at /work/.clang-tidy
WORK = PurePosixPath("/work")  # Where the allowed folders appear inside the sandbox
SEPARATORS = {"|", "||", "&&", ";"}
# git in the shell only looks (and can undo uncommitted edits); committing, branching and syncing
# with GitHub belong to the pr tool, which keeps the repository in the state it expects
GIT_READ_ONLY = {"status", "log", "show", "diff", "blame", "grep", "ls-files", "shortlog", "describe",
                 "rev-parse", "restore"}
GIT_LIST_ONLY = {"branch": {"-a", "-r", "-v", "-vv", "--all", "--remotes", "--list", "-l", "--show-current"},
                 "tag": {"-l", "--list", "-n"}}
# Harmless habits: errors already appear in the output, so these are dropped before the check
STDERR_HABITS = re.compile(r"(?<!\S)2>(&1|/dev/null)(?!\S)")
TIMEOUT = 25
MAX_LINES = 300
MAX_CHARS = 30_000


def load_commands() -> dict[str, dict]:
    """
    Read the allowed commands, leaving out those that are also their own tool.
    Returns:
        dict[str, dict]: Each command and its entry ("about", optional "needs").
    """
    commands = json.loads(COMMANDS_FILE.read_text(encoding="utf-8"))["commands"]
    return {name: entry if isinstance(entry, dict) else {"about": entry} for name, entry in commands.items()}


def own_tool(command: str) -> bool:
    """Whether a command is also a tool of its own (tools/<command>/tool.json), which then wins."""
    return command != "shell" and (fs_gate.TOOLS_DIR / command / "tool.json").is_file()


def to_host(path: str, current: Path, allowed: dict[str, fs_gate.Folder]) -> Optional[Path]:
    """
    Map a path as a command sees it (relative, or under /work) to the real path.
    Args:
        path: The path in the command line.
        current: The real folder the command runs in.
        allowed: The allowed folders.
    Returns:
        Optional[Path]: The resolved real path, or None if it is outside the sandbox's folders.
    """
    if path.startswith("/"):
        parts = PurePosixPath(path).parts
        if len(parts) < 3 or PurePosixPath(*parts[:2]) != WORK or parts[2] not in allowed:
            return None
        target = (allowed[parts[2]].path / Path(*parts[3:])).resolve()
    else:
        target = (current / path).resolve()
    return target if fs_gate.locate(target, allowed) else None


def check(command: str, cwd: Path, commands: dict[str, dict], allowed: dict[str, fs_gate.Folder]) -> None:
    """
    Check a command line before it runs.
    Args:
        command: The command line.
        cwd: The real folder it starts in.
        commands: The allowed commands.
        allowed: The allowed folders.
    Raises:
        ValueError: If any part of it is not allowed; the message says why.
    """
    if any(c in command for c in "\n\r`") or "$(" in command:
        raise ValueError("Line breaks, backticks and $(...) are not allowed; use |, && or ; between commands.")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.commenters = ""  # bash only treats # as a comment at the start of a word; never hide text
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as parse_error:
        raise ValueError(f"Cannot parse the command: {parse_error}") from None

    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in SEPARATORS:
            segments.append([])
        elif token and set(token) <= set("<>&|();"):
            raise ValueError(f"'{token}' is not allowed: no redirection, background jobs, subshells or groups.")
        else:
            segments[-1].append(token)

    current = cwd
    for words in segments:
        if not words:
            raise ValueError("Empty command between separators.")
        name = words[0]
        if "/" in name:  # A program in an allowed folder: needs execute access there
            program = to_host(name, current, allowed)
            located = fs_gate.locate(program, allowed) if program else None
            # The program may not exist yet (make builds it).
            if program is None or located is None or "x" not in located[0].access_at(program):
                raise ValueError(f"'{name}' is not a program in a folder with execute (x) access.")
            continue
        if own_tool(name):
            raise ValueError(f"Use the {name} tool instead of '{name}' in the shell.")
        if name not in commands:
            raise ValueError(f"'{name}' is not an allowed command. Run the command help to see the list.")
        if name == "git":
            sub = words[1] if len(words) > 1 else ""
            if sub in GIT_LIST_ONLY:
                if any(w not in GIT_LIST_ONLY[sub] for w in words[2:]):
                    raise ValueError(f"git {sub} may only list here. To commit or create a branch, use the pr tool.")
            elif sub not in GIT_READ_ONLY:
                raise ValueError(f"git {sub or '(nothing)'} is not allowed in the shell: it only reads ("
                                 f"{', '.join(sorted(GIT_READ_ONLY))}). The pr tool commits, creates the branch "
                                 f"and opens the pull request; pr with action sync updates the repository from GitHub.")
        if name == "cd":
            target = to_host(words[1] if len(words) > 1 else ".", current, allowed)
            if not target or not target.is_dir():
                raise ValueError(f"cd: '{words[1] if len(words) > 1 else ''}' is not a folder in the allowed folders.")
            current = target
        if commands[name].get("needs"):
            located = fs_gate.locate(current, allowed)
            if located is None:
                raise ValueError("The working folder is outside the allowed folders.")
            folder, shown = located
            missing = [r for r in commands[name]["needs"] if r not in folder.access_at(current)]
            if missing:
                raise ValueError(f"{name} needs {'/'.join(missing)} access, which {shown} does not have.")
            if name == "make" and any(w in ("-C", "-f", "--directory", "--file", "--makefile") or w.startswith(
                    ("-C", "-f", "--directory=", "--file=", "--makefile=")) for w in words[1:]):
                raise ValueError(f"{name}: run it where its files are (cd there) instead of using -C or -f.")


def identity_files(folder: Path) -> Path:
    """
    Write a minimal /etc/passwd and /etc/group for the sandbox: only the user running the agent,
    so whoami, ls -l and git know the name without the host's list of accounts.
    Args:
        folder: An empty folder for the two files.
    Returns:
        Path: The folder, holding passwd and group.
    """
    uid, gid = os.getuid(), os.getgid()
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = f"user{uid}"
    try:
        group = grp.getgrgid(gid).gr_name
    except KeyError:
        group = user
    (folder / "passwd").write_text(f"{user}:x:{uid}:{gid}::/tmp/home:/bin/bash\n")
    (folder / "group").write_text(f"{group}:x:{gid}:\n")
    return folder


def sandbox(allowed: dict[str, fs_gate.Folder], cwd: Path, identity: Optional[Path] = None) -> list[str]:
    """
    Build the bubblewrap command line for the allowed folders.
    Args:
        allowed: The allowed folders.
        cwd: The real folder to start in.
        identity: A folder from identity_files, mounted as /etc/passwd and /etc/group; None for none.
    Returns:
        list[str]: bwrap (by full path) and its options, ending with "--" (the command follows).
            Run it with an empty environment: bwrap hands the command its own environment plus
            the --setenv variables, so the command sees only those (as --clearenv would, which
            bubblewrap before 0.5, as in RHEL 9, does not have).
    Raises:
        ValueError: If the working folder is outside the allowed folders.
    """
    args = [shutil.which("bwrap") or "/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/tmp/home"]
    for etc in ("/etc/localtime", "/etc/ld.so.cache", "/etc/alternatives"):
        if Path(etc).exists():
            args += ["--ro-bind", etc, etc]
    if identity is not None:
        args += ["--ro-bind", str(identity / "passwd"), "/etc/passwd", "--ro-bind", str(identity / "group"), "/etc/group"]
    for name, folder in allowed.items():
        mount = str(WORK / name)
        # A folder that does not exist yet (.memory before the first note) has nothing to show
        if "r" not in folder.access or not folder.path.is_dir():
            continue
        args += ["--bind" if "w" in folder.access else "--ro-bind", str(folder.path), mount]
        for sub, access in sorted(folder.subpaths.items(), key=lambda item: len(item[0].parts)):
            if sub.exists():
                inner = str(WORK / name / sub.relative_to(folder.path))
                args += ["--tmpfs", inner] if "r" not in access else \
                        ["--bind" if "w" in access else "--ro-bind", str(sub), inner]
        if "w" in folder.access:  # Nothing planted in a repository may run later, outside the sandbox
            for git in [folder.path / ".git", *folder.path.glob("*/.git")]:
                for protected in ("config", "hooks"):
                    if (git / protected).exists():
                        inner = str(WORK / name / (git / protected).relative_to(folder.path))
                        args += ["--ro-bind", str(git / protected), inner]
    # clang-format and clang-tidy look for their config in each file's parent folders
    for config, name in ((CLANG_FORMAT, ".clang-format"), (CLANG_TIDY, ".clang-tidy")):
        if config.is_file():
            args += ["--ro-bind", str(config), str(WORK / name)]
    located = fs_gate.locate(cwd, allowed)
    if located is None:
        raise ValueError("The working folder is outside the allowed folders.")
    _, shown = located
    args += ["--chdir", str(WORK / shown)]
    environment = {
        "PATH": "/usr/bin:/bin", "HOME": "/tmp/home", "LANG": "C.UTF-8", "TERM": "dumb",
        "PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null",
        "GIT_CONFIG_KEY_1": "core.fsmonitor", "GIT_CONFIG_VALUE_1": "false",
        "GIT_CONFIG_KEY_2": "safe.directory", "GIT_CONFIG_VALUE_2": "*",
    }
    for key, setting in (("GIT_AUTHOR_NAME", "user.name"), ("GIT_AUTHOR_EMAIL", "user.email")):
        value = subprocess.run(["git", "config", "--global", setting], capture_output=True, text=True).stdout.strip()
        if value:
            environment[key] = environment[key.replace("AUTHOR", "COMMITTER")] = value
    for key, value in environment.items():
        args += ["--setenv", key, value]
    return args + ["--"]


def run(cwd: str, command: str) -> tuple[bool, str]:
    """
    Check and run a command line in the sandbox.
    Args:
        cwd: <allowed name>/<folder> to start in; "" for the first allowed folder (context/paths.json),
            for commands that do not depend on a folder (whoami, date, bc).
        command: The command line.
    Returns:
        tuple[bool, str]: Whether it exited with status 0, and its output.
    Raises:
        ValueError: If the folder or the command line is not allowed.
    """
    allowed = fs_gate.load_allowed()
    commands = {name: entry for name, entry in load_commands().items() if not own_tool(name)}
    if command.strip() in ("", "help"):
        return True, help_text(allowed, commands)
    if not cwd.strip():
        cwd = next(iter(allowed))  # The first allowed folder
    folder, shown = fs_gate.resolve(cwd, "dir", "r", allowed)
    command = STDERR_HABITS.sub("", command)
    check(command, folder, commands, allowed)
    if not shutil.which("bwrap"):
        raise ValueError("bubblewrap (bwrap) is not installed, so the shell cannot run safely.")
    try:
        with tempfile.TemporaryDirectory(prefix="shell-identity-") as identity:
            result = subprocess.run([*sandbox(allowed, folder, identity_files(Path(identity))),
                                     "/usr/bin/bash", "--noprofile", "--norc", "-c", command],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                    env={}, timeout=TIMEOUT)  # Nothing of the caller's environment
    except subprocess.TimeoutExpired:
        return False, f"Stopped after {TIMEOUT}s: {command}"
    output = fs_gate.display(result.stdout, allowed).replace(f"{WORK}/", "")
    if len(output) > MAX_CHARS:
        output = output[:MAX_CHARS] + "\n..."
    lines = output.rstrip("\n").splitlines()
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES] + [f"... {len(lines) - MAX_LINES} more lines; narrow the command (head, grep)"]
    if result.returncode != 0:
        lines.append(f"[exit {result.returncode}]")
    return result.returncode == 0, "\n".join(lines) if lines else f"(no output) in {shown}"


def help_text(allowed: dict[str, fs_gate.Folder], commands: dict[str, dict]) -> str:
    """
    Describe the folders and commands available.
    Args:
        allowed: The allowed folders.
        commands: The allowed commands (without those that are their own tools).
    Returns:
        str: The allowed folders with their access, then each command and its note.
    """
    width = max(map(len, commands))
    lines = [fs_gate.describe(allowed), "", "Commands (join them with |, &&, || or ;):"]
    lines += [f"{name:<{width}}  {entry['about']}" + (f" [needs {entry['needs']}]" if entry.get("needs") else "")
              for name, entry in sorted(commands.items())]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> tuple[bool, str]:
    """
    Read "--cwd <folder> --command <line>" (values taken verbatim, as the agents pass them) and run.
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        tuple[bool, str]: Whether the command succeeded, and its output.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    options = {}
    while argv:
        argument = argv.pop(0)
        if argument in ("--cwd", "--command") and argv:
            options[argument[2:]] = argv.pop(0)
        else:
            raise ValueError(f"Unexpected argument '{argument}'; use --cwd <folder> --command <command line>.")
    return run(options.get("cwd", ""), options.get("command", ""))


if __name__ == "__main__":
    try:
        ok, report = main()
    except (ValueError, OSError) as e:
        ok, report = False, f"Error: {e}"
    print(report)
    sys.exit(0 if ok else 1)
