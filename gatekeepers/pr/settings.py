"""
Module: settings.py

Description:
    `GateSettings`: the pull request gate's settings, read once when the service or a command
    starts. Each comes from the environment first, then settings.json, which the service and the
    agents' pr_gate tool share: it is the place to change them.

    `Project`: one gated repository. The folders marked "pr_gated" in context/paths.json are the
    gated projects; each must hold a git clone whose origin is on GitHub, which names the
    repository (owner/name). settings.json's "projects" may override the build settings per folder.
"""

import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from gatekeepers.fs.fs_gate import FsGate
from gatekeepers.pr import DATA_DIR, SETTINGS_FILE

# A GitHub remote: https://github.com/owner/name(.git), git@github.com:owner/name(.git) or
# ssh://git@github.com/owner/name(.git)
GITHUB_REMOTE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                           r"(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$")


@dataclass(frozen=True)
class Project:
    """
    One gated repository: a folder of context/paths.json marked "pr_gated".
    """

    name: str  # The folder's name in paths.json, e.g. core_dump
    repo: str  # owner/name on GitHub, from the clone's origin
    path: Path = Path()  # The local clone, kept current with GitHub
    build_command: str = "make"  # QUIZ_BUILD_COMMAND; "" skips the build check
    test_target: str = "check"  # QUIZ_TEST_TARGET
    fail_on_warnings: bool = True  # QUIZ_FAIL_ON_WARNINGS

    @staticmethod
    def github_repo(path: Path) -> str:
        """
        Name the GitHub repository a clone comes from.
        Args:
            path: The clone.
        Returns:
            str: owner/name.
        Raises:
            ValueError: If the folder is not a git clone, or its origin is not on GitHub.
        """
        try:
            result = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True,
                                    text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"cannot read {path}'s origin: {exc}") from None
        if result.returncode != 0:
            raise ValueError(f"{path} is not a git clone with an origin remote.")
        url = result.stdout.strip()
        match = GITHUB_REMOTE.match(url)
        if not match:
            raise ValueError(f"its origin, {url}, is not a github.com repository; only GitHub is supported.")
        return f"{match['owner']}/{match['name']}"


@dataclass(frozen=True)
class GateSettings:
    """
    The gate's settings (README.md, "Settings").
    """

    base_url: str  # QUIZ_BASE_URL, with {hostname} filled in: where the quiz pages are served, without a trailing /
    developer: str = ""  # QUIZ_DEVELOPER: the only author assessed; "" for the account gh is signed in as
    projects: tuple[Project, ...] = ()  # The gated repositories (pr_gated in context/paths.json)
    skipped: tuple[str, ...] = ()  # Gated folders that cannot be used, each with why
    poll_seconds: float = 5  # QUIZ_POLL_SECONDS: each idle poll costs one GitHub API request per project
    profile: Optional[str] = None  # QUIZ_MODEL_PROFILE; None uses the models file's default
    web_user: str = "user"  # QUIZ_WEB_USER: demo sign-in, shown on the sign-in page
    web_password: str = "pass"  # QUIZ_WEB_PASSWORD
    sync_seconds: float = 30  # QUIZ_SYNC_SECONDS: how often the clones are fast-forwarded; 0 never
    pr_comment: bool = True  # QUIZ_PR_COMMENT: keep a comment on the pull request
    allow_skip: bool = False  # QUIZ_ALLOW_SKIP: proof-of-concept mode
    data_dir: Path = DATA_DIR  # QUIZ_DATA_DIR: the quiz database and the secret key

    @classmethod
    def load(cls, path: Path = SETTINGS_FILE, paths: Optional[Path] = None) -> "GateSettings":
        """
        Read every setting, and the gated projects.
        Args:
            path: The settings file (default: gatekeepers/pr/settings.json).
            paths: The allowed folders; None uses FS_GATE_PATHS, then context/paths.json.
        Returns:
            GateSettings: The settings.
        Raises:
            ValueError: If a required setting is missing.
        """
        defaults = json.loads(path.read_text(encoding="utf-8")).get("settings", {})

        def text(name: str, required: bool = False) -> str:
            return cls.pick(defaults, name, required, path)

        def flag(name: str) -> bool:
            return text(name).lower() in ("true", "1", "yes")

        projects, skipped = [], []
        for folder in FsGate.load(paths).folders.values():
            if not folder.pr_gated:
                continue
            try:
                projects.append(Project(folder.name, Project.github_repo(folder.path), folder.path,
                                        **cls.build_options(folder.name, path)))
            except ValueError as exc:
                skipped.append(f"{folder.name}: {exc}")
        return cls(
            base_url=cls.base_url_for(text("QUIZ_BASE_URL", True)),
            developer=text("QUIZ_DEVELOPER"),
            projects=tuple(projects),
            skipped=tuple(skipped),
            poll_seconds=float(text("QUIZ_POLL_SECONDS", True)),
            profile=text("QUIZ_MODEL_PROFILE") or None,
            web_user=text("QUIZ_WEB_USER", True),
            web_password=text("QUIZ_WEB_PASSWORD", True),
            sync_seconds=float(text("QUIZ_SYNC_SECONDS") or 30),
            pr_comment=flag("QUIZ_PR_COMMENT"),
            allow_skip=flag("QUIZ_ALLOW_SKIP"),
            data_dir=Path(os.environ.get("QUIZ_DATA_DIR") or DATA_DIR),
        )

    @classmethod
    def build_options(cls, name: str, path: Path = SETTINGS_FILE) -> dict[str, Any]:
        """
        One project's build settings: its entry in settings.json's "projects", else the shared
        QUIZ_BUILD_COMMAND, QUIZ_TEST_TARGET and QUIZ_FAIL_ON_WARNINGS.
        Args:
            name: The project's folder name in context/paths.json, e.g. core_dump.
            path: The settings file.
        Returns:
            dict[str, Any]: build_command, test_target and fail_on_warnings.
        """
        content = json.loads(path.read_text(encoding="utf-8"))
        own = content.get("projects", {}).get(name, {})

        def text(setting: str) -> str:
            return str(own[setting]) if setting in own else cls.pick(content.get("settings", {}), setting, False, path)

        return {"build_command": text("QUIZ_BUILD_COMMAND"), "test_target": text("QUIZ_TEST_TARGET"),
                "fail_on_warnings": text("QUIZ_FAIL_ON_WARNINGS").lower() in ("true", "1", "yes")}

    @staticmethod
    def base_url_for(value: str) -> str:
        """
        The quiz pages' address from QUIZ_BASE_URL: {hostname} becomes the name of the machine the
        service runs on (as socket.gethostname gives it), so the shipped value works on any host; an
        explicit host stays as it is.
        Args:
            value: The setting, e.g. "http://{hostname}:8000" or "http://minion:8000".
        Returns:
            str: The address, without a trailing /.
        """
        return value.replace("{hostname}", socket.gethostname()).rstrip("/")

    @classmethod
    def setting(cls, name: str, required: bool = True, path: Path = SETTINGS_FILE) -> str:
        """
        Read one setting.
        Args:
            name: The setting, e.g. "QUIZ_BASE_URL".
            required: Fail if neither place sets it.
            path: The settings file.
        Returns:
            str: The value; "" for an optional setting that is not set.
        Raises:
            ValueError: If a required setting is missing.
        """
        return cls.pick(json.loads(path.read_text(encoding="utf-8")).get("settings", {}), name, required, path)

    @staticmethod
    def pick(defaults: dict[str, Any], name: str, required: bool, path: Path) -> str:
        """
        Choose one setting's value: the environment's, else the file's.
        Args:
            defaults: The file's "settings".
            name: The setting.
            required: Fail if neither place sets it.
            path: The settings file, for the error.
        Returns:
            str: The value; "" for an optional setting that is not set.
        Raises:
            ValueError: If a required setting is missing.
        """
        value = os.environ.get(name) or defaults.get(name, "")
        if required and not value:
            raise ValueError(f"Set {name} in {path} (or in the environment).")
        return value
