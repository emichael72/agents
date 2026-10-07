"""
Module: settings.py

Description:
    `GateSettings`: the pull request gate's settings, read once when the service or a command
    starts. Each comes from the environment first, then settings.json, which the service and the
    agents' pr_gate tool share: it is the place to change them.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from gatekeepers.pr import DATA_DIR, SETTINGS_FILE


@dataclass(frozen=True)
class GateSettings:
    """
    The gate's settings (README.md, "Settings").
    """

    repo: str  # QUIZ_REPO: owner/name of the gated repository
    developer: str  # QUIZ_DEVELOPER: the only author whose pull requests are assessed
    base_url: str  # QUIZ_BASE_URL: where the quiz pages are served, without a trailing /
    poll_seconds: float = 5  # QUIZ_POLL_SECONDS: each idle poll costs one GitHub API request
    profile: Optional[str] = None  # QUIZ_MODEL_PROFILE; None uses the models file's default
    web_user: str = "user"  # QUIZ_WEB_USER: demo sign-in, shown on the sign-in page
    web_password: str = "pass"  # QUIZ_WEB_PASSWORD
    build_command: str = "make"  # QUIZ_BUILD_COMMAND; "" skips the build check
    test_target: str = "check"  # QUIZ_TEST_TARGET
    fail_on_warnings: bool = True  # QUIZ_FAIL_ON_WARNINGS
    local_clone: str = ""  # QUIZ_LOCAL_CLONE: kept current with GitHub; "" for none
    sync_seconds: float = 30  # QUIZ_SYNC_SECONDS
    pr_comment: bool = True  # QUIZ_PR_COMMENT: keep a comment on the pull request
    allow_skip: bool = False  # QUIZ_ALLOW_SKIP: proof-of-concept mode
    data_dir: Path = DATA_DIR  # QUIZ_DATA_DIR: the quiz database and the secret key

    @classmethod
    def load(cls, path: Path = SETTINGS_FILE) -> "GateSettings":
        """
        Read every setting.
        Args:
            path: The settings file (default: gatekeepers/pr/settings.json).
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

        return cls(
            repo=text("QUIZ_REPO", True),
            developer=text("QUIZ_DEVELOPER", True),
            base_url=text("QUIZ_BASE_URL", True).rstrip("/"),
            poll_seconds=float(text("QUIZ_POLL_SECONDS", True)),
            profile=text("QUIZ_MODEL_PROFILE") or None,
            web_user=text("QUIZ_WEB_USER", True),
            web_password=text("QUIZ_WEB_PASSWORD", True),
            build_command=text("QUIZ_BUILD_COMMAND"),
            test_target=text("QUIZ_TEST_TARGET"),
            fail_on_warnings=flag("QUIZ_FAIL_ON_WARNINGS"),
            local_clone=text("QUIZ_LOCAL_CLONE"),
            sync_seconds=float(text("QUIZ_SYNC_SECONDS") or 30),
            pr_comment=flag("QUIZ_PR_COMMENT"),
            allow_skip=flag("QUIZ_ALLOW_SKIP"),
            data_dir=Path(os.environ.get("QUIZ_DATA_DIR") or DATA_DIR),
        )

    @classmethod
    def setting(cls, name: str, required: bool = True, path: Path = SETTINGS_FILE) -> str:
        """
        Read one setting.
        Args:
            name: The setting, e.g. "QUIZ_REPO".
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
