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
      - Every command (after |, ||, && or ;) must be in commands.json and installed (help lists
        only those), or be a program inside a folder with "x" access (./core_dump). A command that
        is also its own tool is refused: the dedicated tool wins.
      - No redirection (< >), background (&), subshells or groups, $(...) or backticks, or
        line breaks. "2>&1" and "2>/dev/null" are dropped: errors already appear in the output.
      - Commands marked "needs": "x" (make, ninja) need execute access where they run; cd is
        followed to know where that is. make and ninja must run where their build files are.
    The check is stricter than bash, never looser: it may refuse an unusual line, but it cannot
    pass a line in which bash would find a command it did not see.

    Also: git runs with hooks and fsmonitor off, and .git/config and .git/hooks stay read-only in
    writable folders, so nothing planted in a repository runs later outside the sandbox. Output
    shows /work/<name> as <name>; it stops after TIMEOUT seconds and MAX_LINES lines.
"""

import argparse
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
from typing import Any, Optional

# Import the shared filesystem gate from the repository root (the nearest folder above
# holding pyproject.toml).
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents
                            if (p / "pyproject.toml").is_file())))
from gatekeepers import CONTEXT_DIR, TOOLS_DIR
from gatekeepers.fs.fs_gate import FsGate
from tools.common.cli import ToolArgumentParser


class Shell:
    """
    Checks command lines against the allowed commands and folders, and runs them in the sandbox.
    """

    VERSION = "1.0.0"
    COMMANDS_FILE = Path(__file__).resolve().parent / "commands.json"
    PROGRAMS = "/usr/bin"  # Where the sandbox finds programs (its PATH; /bin links here)
    # The sandbox's own environment. commands.json's "environment" may add search folders and
    # variables, but not replace these or git's settings (hooks off), which the sandbox relies on.
    ENVIRONMENT = {
        "PATH": "/usr/bin:/bin", "HOME": "/tmp/home", "LANG": "C.UTF-8", "TERM": "dumb",
        "PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null",
        "GIT_CONFIG_KEY_1": "core.fsmonitor", "GIT_CONFIG_VALUE_1": "false",
        "GIT_CONFIG_KEY_2": "safe.directory", "GIT_CONFIG_VALUE_2": "*",
    }
    RESERVED_PREFIXES = ("GIT_", "LD_", "BASH_")  # Also kept from commands.json's variables
    VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)}")  # ${NAME} in a variable's value
    BUILTINS = {"cd", "echo", "false", "printf", "pwd", "test", "true"}  # bash's own: always there
    CLANG_FORMAT = CONTEXT_DIR / "clang-format.yaml"  # The default C/C++ style, at /work/.clang-format
    CLANG_TIDY = CONTEXT_DIR / "clang-tidy.yaml"  # The default C/C++ checks, at /work/.clang-tidy
    WORK = PurePosixPath("/work")  # Where the allowed folders appear inside the sandbox
    SEPARATORS = {"|", "||", "&&", ";"}
    # git in the shell only looks (and can undo uncommitted edits); committing, branching and syncing
    # with GitHub belong to the pr tool, which keeps the repository in the state it expects
    GIT_READ_ONLY = {"status", "log", "show", "diff", "blame", "grep", "ls-files", "shortlog", "describe",
                     "rev-parse", "restore"}
    GIT_LIST_ONLY = {"branch": {"-a", "-r", "-v", "-vv", "--all", "--remotes", "--list", "-l", "--show-current"},
                     "tag": {"-l", "--list", "-n"}}
    # The only variables a command line may set, each only before its command: build flags for make
    # (CFLAGS='-DX' make; set in the environment, the Makefile's own "CFLAGS += ..." still applies, as
    # it does not to make CFLAGS=...), and a time zone for date (TZ=Asia/Tokyo date)
    COMMAND_VARIABLES = {"make": ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS", "LDLIBS"), "date": ("TZ",)}
    # What a network command needs from /etc: name lookup, and the certificates HTTPS checks against
    NETWORK_FILES = ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/host.conf", "/etc/gai.conf",
                     "/etc/pki", "/etc/ssl", "/etc/crypto-policies")
    ZONEINFO = Path("/usr/share/zoneinfo")  # The IANA time zones TZ may name; the sandbox sees /usr
    # Options that would point a build tool at another folder's build files, which execute access
    # where it runs does not cover: the build runs where its files are (cd there)
    ELSEWHERE_OPTIONS = {"make": ("-C", "-f", "--directory", "--file", "--makefile"), "ninja": ("-C", "-f")}
    ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
    OPERATOR_CHARACTERS = "();<>|&"  # shlex's punctuation_chars
    # Harmless habits: errors already appear in the output, so these are dropped before the check
    STDERR_HABITS = re.compile(r"(?<!\S)2>(&1|/dev/null)(?!\S)")
    TIMEOUT = 25
    MAX_LINES = 300
    MAX_CHARS = 30_000

    def __init__(self, gate: Optional[FsGate] = None, commands: Optional[dict[str, dict]] = None,
                 allow_network: bool = False) -> None:
        """
        Args:
            gate: The allowed folders; None reads context/paths.json.
            allow_network: Let a command line that uses a network command (commands.json) reach the
                network; the agents' shell tool allows it (SHELL_NETWORK=1), the pull request gate does not.
            commands: The allowed commands; None reads commands.json, leaving out those that are
                also their own tool and those not installed on this machine.
        Raises:
            ValueError: If commands.json's "environment" is not valid.
        """
        self.gate = gate or FsGate.load()
        self.allow_network = allow_network
        environment = self.load_environment()
        self.search_path = self.search_folders(environment.get("path", []))
        self.variables = self.extra_variables(environment.get("variables", {}))
        self.missing: set[str] = set()  # Listed in commands.json, but not installed here
        if commands is None:
            listed = {name: entry for name, entry in self.load_commands().items() if not self.own_tool(name)}
            commands = {name: entry for name, entry in listed.items() if self.installed(name)}
            self.missing = set(listed) - set(commands)
        self.commands: dict[str, dict] = commands

    @classmethod
    def load_commands(cls) -> dict[str, dict]:
        """
        Read the allowed commands, leaving out those that are also their own tool.
        Returns:
            dict[str, dict]: Each command and its entry ("about", optional "needs" and "network").
        """
        commands = json.loads(cls.COMMANDS_FILE.read_text(encoding="utf-8"))["commands"]
        return {name: entry if isinstance(entry, dict) else {"about": entry} for name, entry in commands.items()}

    @classmethod
    def load_environment(cls) -> dict[str, Any]:
        """
        Read what commands.json adds to the sandbox's environment.
        Returns:
            dict[str, Any]: {"path": [folders], "variables": {name: value}}; {} when it adds nothing.
        """
        return json.loads(cls.COMMANDS_FILE.read_text(encoding="utf-8")).get("environment", {})

    def search_folders(self, folders: list[str]) -> list[tuple[str, Path]]:
        """
        Resolve the extra folders to search for commands, as the sandbox sees them: under /usr, or
        under /work in an allowed folder with execute access. One that does not exist here, or
        lacks that access, is left out.
        Args:
            folders: The folders, e.g. ["/usr/local/bin", "/work/core_dump/scripts"].
        Returns:
            list[tuple[str, Path]]: Each folder as the sandbox sees it, and its real path.
        Raises:
            ValueError: If a folder is not an absolute path under /usr or /work.
        """
        found = []
        for folder in folders:
            inner = PurePosixPath(folder)
            if not inner.is_absolute() or ".." in inner.parts or inner.parts[1:2] not in (("usr",), ("work",)):
                raise ValueError(f"commands.json: the search folder '{folder}' must be an absolute path under "
                                 f"/usr or /work.")
            real = self.to_host(folder, Path("/")) if inner.parts[1] == "work" else Path(folder)
            if real is None or not real.is_dir():
                continue
            if inner.parts[1] == "work":  # Its programs run as the folder's own code: needs x
                located = self.gate.locate(real)
                if located is None or "x" not in located[0].access_at(real):
                    continue
            found.append((folder, real))
        return found

    def extra_variables(self, variables: dict[str, Any]) -> dict[str, str]:
        """
        Read the variables to export in the sandbox. ${NAME} in a value is replaced by NAME from this
        tool's own environment (the agents set AGENT_NAME), or by "" when it is not set.
        Args:
            variables: Each variable's name and value, e.g. {"AGENT_NAME": "${AGENT_NAME}"}.
        Returns:
            dict[str, str]: The variables, their references replaced.
        Raises:
            ValueError: If a name is not valid, the sandbox sets it itself, or a value is not text.
        """
        exported = {}
        for name, value in variables.items():
            if not self.VARIABLE_NAME.match(name) or not isinstance(value, str):
                raise ValueError(f"commands.json: the variable '{name}' needs a valid name and a text value.")
            if name in self.ENVIRONMENT or name.startswith(self.RESERVED_PREFIXES):
                raise ValueError(f"commands.json: the sandbox sets '{name}' itself; add search folders with "
                                 f"\"path\".")
            exported[name] = self.REFERENCE.sub(lambda match: os.environ.get(match.group(1), ""), value)
        return exported

    def installed(self, command: str) -> bool:
        """
        Whether the sandbox can run a command: a bash builtin, or a program in the extra search
        folders or PROGRAMS.
        Args:
            command: The command's name, e.g. ctags.
        Returns:
            bool: True when it is there.
        """
        folders = [str(real) for _, real in self.search_path] + [self.PROGRAMS]
        return command in self.BUILTINS or shutil.which(command, path=os.pathsep.join(folders)) is not None

    @staticmethod
    def own_tool(command: str) -> bool:
        """Whether a command is also a tool of its own (tools/<command>/tool.json), which then wins."""
        return command != "shell" and (TOOLS_DIR / command / "tool.json").is_file()

    def to_host(self, path: str, current: Path) -> Optional[Path]:
        """
        Map a path as a command sees it (relative, or under /work) to the real path.
        Args:
            path: The path in the command line.
            current: The real folder the command runs in.
        Returns:
            Optional[Path]: The resolved real path, or None if it is outside the sandbox's folders.
        """
        if path.startswith("/"):
            parts = PurePosixPath(path).parts
            if len(parts) < 3 or PurePosixPath(*parts[:2]) != self.WORK or parts[2] not in self.gate.folders:
                return None
            target = (self.gate.folders[parts[2]].path / Path(*parts[3:])).resolve()
        else:
            target = (current / path).resolve()
        return target if self.gate.locate(target) else None

    def check(self, command: str, cwd: Path) -> list[str]:
        """
        Check a command line before it runs.
        Args:
            command: The command line.
            cwd: The real folder it starts in.
        Returns:
            list[str]: The allowed commands it runs (not programs in the allowed folders).
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
            if token in self.SEPARATORS:
                segments.append([])
            elif token and set(token) <= set("<>&|();"):
                raise ValueError(f"'{token}' is not allowed: no redirection, background jobs, subshells or groups.")
            else:
                segments[-1].append(token)

        commands = self.commands
        current = cwd
        used: list[str] = []
        for segment_index, words in enumerate(segments):
            if not words:
                raise ValueError("Empty command between separators.")
            assigned: dict[str, str] = {}
            while words and (assignment := self.ASSIGNMENT.match(words[0])) is not None:
                assigned[assignment.group(1)] = words[0][assignment.end():]
                words = words[1:]
            if assigned and not self.bare_assignments(command, len(assigned), segment_index):
                raise ValueError("Write a variable before its command as NAME=value, with no quotes or backslashes "
                                 "in NAME= (CFLAGS='-DDEBUG -O0' make).")
            if assigned:
                allowed = self.COMMAND_VARIABLES.get(words[0], ()) if words else ()
                refused = [v for v in assigned if v not in allowed]
                if refused or not words:
                    raise ValueError(f"Only {', '.join(self.COMMAND_VARIABLES['make'])} may be set before make "
                                     f"(CFLAGS='-DDEBUG' make), and TZ before date (TZ=Asia/Tokyo date), "
                                     f"not {' '.join(refused or assigned)}.")
                if "TZ" in assigned and not self.known_time_zone(assigned["TZ"]):
                    raise ValueError(f"Unknown time zone '{assigned['TZ']}': use an IANA name such as Europe/London, "
                                     f"or UTC.")
            name = words[0]
            if "/" in name:  # A program in an allowed folder: needs execute access there
                program = self.to_host(name, current)
                located = self.gate.locate(program) if program else None
                # The program may not exist yet (make builds it).
                if program is None or located is None or "x" not in located[0].access_at(program):
                    raise ValueError(f"'{name}' is not a program in a folder with execute (x) access.")
                continue
            if self.own_tool(name):
                raise ValueError(f"Use the {name} tool instead of '{name}' in the shell.")
            if name in self.missing:
                raise ValueError(f"'{name}' is not installed on this machine. Run the command help to see the list.")
            if name not in commands:
                raise ValueError(f"'{name}' is not an allowed command. Run the command help to see the list.")
            used.append(name)
            if name == "git":
                sub = words[1] if len(words) > 1 else ""
                if sub in self.GIT_LIST_ONLY:
                    if any(w not in self.GIT_LIST_ONLY[sub] for w in words[2:]):
                        raise ValueError(f"git {sub} may only list here. To commit or create a branch, use the pr tool.")
                elif sub not in self.GIT_READ_ONLY:
                    raise ValueError(f"git {sub or '(nothing)'} is not allowed in the shell: it only reads ("
                                     f"{', '.join(sorted(self.GIT_READ_ONLY))}). The pr tool commits, creates the branch "
                                     f"and opens the pull request; pr with action sync updates the repository from GitHub.")
            if name == "cd":
                target = self.to_host(words[1] if len(words) > 1 else ".", current)
                if not target or not target.is_dir():
                    raise ValueError(f"cd: '{words[1] if len(words) > 1 else ''}' is not a folder in the allowed folders.")
                current = target
            if commands[name].get("needs"):
                located = self.gate.locate(current)
                if located is None:
                    raise ValueError("The working folder is outside the allowed folders.")
                folder, shown = located
                missing = [r for r in commands[name]["needs"] if r not in folder.access_at(current)]
                if missing:
                    raise ValueError(f"{name} needs {'/'.join(missing)} access, which {shown} does not have.")
                elsewhere = self.ELSEWHERE_OPTIONS.get(name, ())
                prefixes = tuple(o if len(o) == 2 else o + "=" for o in elsewhere)
                if elsewhere and any(w in elsewhere or w.startswith(prefixes) for w in words[1:]):
                    raise ValueError(f"{name}: run it where its files are (cd there) instead of using -C or -f.")
        return used

    @classmethod
    def raw_words(cls, command: str) -> list[str]:
        """
        Split a command line into words as written, quotes and backslashes kept: unquoted whitespace
        ends a word, and a run of unquoted operator characters ( ) ; < > | & is a word of its own,
        as shlex with punctuation_chars splits it.
        Args:
            command: The command line.
        Returns:
            list[str]: The words.
        """
        words: list[str] = []
        word, quote, i = "", "", 0
        while i < len(command):
            c = command[i]
            if quote:
                word += c
                if c == "\\" and quote == '"' and i + 1 < len(command):
                    word, i = word + command[i + 1], i + 1
                elif c == quote:
                    quote = ""
            elif c == "\\" and i + 1 < len(command):
                word, i = word + c + command[i + 1], i + 1
            elif c in "'\"":
                word, quote = word + c, c
            elif c.isspace() or c in cls.OPERATOR_CHARACTERS:
                if word:
                    words.append(word)
                word = ""
                if not c.isspace():
                    run = c
                    while i + 1 < len(command) and command[i + 1] in cls.OPERATOR_CHARACTERS:
                        run, i = run + command[i + 1], i + 1
                    words.append(run)
            else:
                word += c
            i += 1
        if word:
            words.append(word)
        return words

    @classmethod
    def known_time_zone(cls, zone: str) -> bool:
        """
        Whether a TZ value names an installed IANA time zone, such as Asia/Tokyo or UTC.
        Args:
            zone: The value, as written after TZ=.
        Returns:
            bool: True for a zone file under ZONEINFO.
        """
        parts = PurePosixPath(zone).parts
        if not parts or zone.startswith("/") or ".." in parts:
            return False
        return (cls.ZONEINFO / zone).is_file()

    @classmethod
    def bare_assignments(cls, command: str, count: int, segment_index: int) -> bool:
        """
        Whether a command's first `count` words are assignments as bash sees them: NAME= written
        bare. A quoted or escaped word such as "CFLAGS=/x" reads as an assignment once its quotes
        are removed, but bash runs it as a command.
        Args:
            command: The whole command line.
            count: How many leading assignments the check found in the command.
            segment_index: Which command of the line (0 for the first, after the separators).
        Returns:
            bool: True when the written words agree.
        """
        segments: list[list[str]] = [[]]
        for word in cls.raw_words(command):
            if word in cls.SEPARATORS:
                segments.append([])
            else:
                segments[-1].append(word)
        if segment_index >= len(segments) or len(segments[segment_index]) < count:
            return False
        return all(cls.ASSIGNMENT.match(word) for word in segments[segment_index][:count])

    @staticmethod
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

    def sandbox(self, cwd: Path, identity: Optional[Path] = None, network: bool = False) -> list[str]:
        """
        Build the bubblewrap command line for the allowed folders.
        Args:
            cwd: The real folder to start in.
            identity: A folder from identity_files, mounted as /etc/passwd and /etc/group; None for none.
            network: Share the host's network, with what name lookup and HTTPS need from /etc (for a
                network command such as curl); otherwise the sandbox has no network.
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
        if network:
            args[2:2] = ["--share-net"]  # After --unshare-all: every namespace but the network's stays private
            for etc in self.NETWORK_FILES:
                if Path(etc).exists():  # resolv.conf is often a link into /run: bind what it points to
                    args += ["--ro-bind", str(Path(etc).resolve()), etc]
        if identity is not None:
            args += ["--ro-bind", str(identity / "passwd"), "/etc/passwd", "--ro-bind", str(identity / "group"), "/etc/group"]
        for name, folder in self.gate.folders.items():
            mount = str(self.WORK / name)
            # A folder that does not exist yet (.memory before the first note) has nothing to show
            if "r" not in folder.access or not folder.path.is_dir():
                continue
            args += ["--bind" if "w" in folder.access else "--ro-bind", str(folder.path), mount]
            for sub, access in sorted(folder.subpaths.items(), key=lambda item: len(item[0].parts)):
                if sub.exists():
                    inner = str(self.WORK / name / sub.relative_to(folder.path))
                    args += ["--tmpfs", inner] if "r" not in access else \
                            ["--bind" if "w" in access else "--ro-bind", str(sub), inner]
            if "w" in folder.access:  # Nothing planted in a repository may run later, outside the sandbox
                for git in [folder.path / ".git", *folder.path.glob("*/.git")]:
                    for protected in ("config", "hooks"):
                        if (git / protected).exists():
                            inner = str(self.WORK / name / (git / protected).relative_to(folder.path))
                            args += ["--ro-bind", str(git / protected), inner]
        # clang-format and clang-tidy look for their config in each file's parent folders
        for config, name in ((self.CLANG_FORMAT, ".clang-format"), (self.CLANG_TIDY, ".clang-tidy")):
            if config.is_file():
                args += ["--ro-bind", str(config), str(self.WORK / name)]
        located = self.gate.locate(cwd)
        if located is None:
            raise ValueError("The working folder is outside the allowed folders.")
        _, shown = located
        args += ["--chdir", str(self.WORK / shown)]
        environment = {
            **self.ENVIRONMENT,
            **self.variables,
            "PATH": ":".join([inner for inner, _ in self.search_path] + [self.ENVIRONMENT["PATH"]]),
        }
        for key, setting in (("GIT_AUTHOR_NAME", "user.name"), ("GIT_AUTHOR_EMAIL", "user.email")):
            value = subprocess.run(["git", "config", "--global", setting], capture_output=True, text=True).stdout.strip()
            if value:
                environment[key] = environment[key.replace("AUTHOR", "COMMITTER")] = value
        for key, value in environment.items():
            args += ["--setenv", key, value]
        return args + ["--"]

    def run(self, cwd: str, command: str) -> tuple[bool, str]:
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
        if command.strip() in ("", "help"):
            return True, self.help_text()
        if not cwd.strip():
            cwd = next(iter(self.gate.folders))  # The first allowed folder
        folder, shown = self.gate.resolve(cwd, "dir", "r")
        command = self.STDERR_HABITS.sub("", command)
        used = self.check(command, folder)
        network = self.allow_network and any(self.commands[name].get("network") for name in used)
        if not shutil.which("bwrap"):
            raise ValueError("bubblewrap (bwrap) is not installed, so the shell cannot run safely.")
        try:
            with tempfile.TemporaryDirectory(prefix="shell-identity-") as identity:
                result = subprocess.run([*self.sandbox(folder, self.identity_files(Path(identity)), network),
                                         "/usr/bin/bash", "--noprofile", "--norc", "-c", command],
                                        stdin=subprocess.DEVNULL,  # No input: rg or cat with no file must not wait
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                        env={}, timeout=self.TIMEOUT)  # Nothing of the caller's environment
        except subprocess.TimeoutExpired:
            return False, f"Stopped after {self.TIMEOUT}s: {command}"
        output = self.gate.display(result.stdout).replace(f"{self.WORK}/", "")
        if len(output) > self.MAX_CHARS:
            output = output[:self.MAX_CHARS] + "\n..."
        lines = output.rstrip("\n").splitlines()
        if len(lines) > self.MAX_LINES:
            lines = lines[:self.MAX_LINES] + [f"... {len(lines) - self.MAX_LINES} more lines; narrow the command (head, grep)"]
        if result.returncode != 0:
            lines.append(f"[exit {result.returncode}]")
        return result.returncode == 0, "\n".join(lines) if lines else f"(no output) in {shown}"

    def help_text(self) -> str:
        """
        Describe the folders and commands available.
        Returns:
            str: The allowed folders with their access, then each command and its note.
        """
        commands = self.commands
        width = max(map(len, commands))
        lines = [self.gate.describe(), "", "Commands (join them with |, &&, || or ;):"]
        lines += [f"{name:<{width}}  {entry['about']}" + (f" [needs {entry['needs']}]" if entry.get("needs") else "")
                  for name, entry in sorted(commands.items())]
        return "\n".join(lines)

    @classmethod
    def build_parser(cls) -> ToolArgumentParser:
        """
        The command line: "[--cwd=<folder>] --command=<command line>".
        Returns:
            ToolArgumentParser: The parser.
        """
        parser = ToolArgumentParser("shell", cls.VERSION, "Run a command line in the sandbox.")
        parser.add_argument("--cwd", default="", help="<allowed name>/<folder> to run in; omit it for the first "
                                                      "allowed folder")
        parser.add_argument("--command", default="", help="The command line; help lists the commands")
        return parser


def main(argv: Optional[list[str]] = None) -> int:
    """
    Parse the command line, run the command and print its output, or "Error: <reason>".
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        int: 0 when the command exited with status 0, 1 otherwise.
    """
    try:
        args: argparse.Namespace = Shell.build_parser().parse_args(argv)
        ok, report = Shell(allow_network=os.environ.get("SHELL_NETWORK") == "1").run(args.cwd, args.command)
    except (ValueError, OSError) as e:
        ok, report = False, f"Error: {e}"
    print(report)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
