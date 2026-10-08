"""
Module: skill.py

Description:
    The agents' skills: step-by-step procedures for kinds of tasks, one <name>/SKILL.md per skill in
    the folder context/agent.json names (skills_dir, agents/skills). The agents list every skill's
    name and description in their instructions at start-up; this tool gives the model a skill's
    full text when a task matches it.

    Actions:
      - read (default): a skill's procedure, or, with no name, the list of skills.

    Key design points:
      - Read-only: the model follows skills, it never writes them; people add them.
      - Skill names are plain (letters, digits, - and _), so a name never leaves the skills folder.
      - A SKILL.md opens with a header between --- lines holding "name:" and "description:"; the
        description is the one line the agents list, so it says when to use the skill.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
sys.path.insert(0, str(REPO_ROOT))
from tools.common.cli import ToolArgumentParser  # noqa: E402


class Skills:
    """
    The skills in the skills folder.
    """

    VERSION = "1.0.0"
    NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

    def __init__(self, folder: Optional[Path] = None) -> None:
        """
        Args:
            folder: The skills folder; None reads skills_dir from context/agent.json.
        """
        if folder is None:
            settings = json.loads((REPO_ROOT / "context" / "agent.json").read_text(encoding="utf-8"))
            folder = REPO_ROOT / settings.get("skills_dir", "skills")
        self.folder = folder

    @staticmethod
    def header(text: str) -> tuple[dict[str, str], str]:
        """
        Split a SKILL.md into its header fields and its body.
        Args:
            text: The file's text.
        Returns:
            tuple[dict[str, str], str]: The "key: value" fields between the opening --- lines, and the
                rest of the file; no fields and the whole text when there is no header.
        """
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---" or "---" not in [line.strip() for line in lines[1:]]:
            return {}, text
        end = 1 + [line.strip() for line in lines[1:]].index("---")
        fields = {}
        for line in lines[1:end]:
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip()
        return fields, "\n".join(lines[end + 1:]).strip() + "\n"

    def list(self) -> str:
        """
        The skills, one line each: "- <name>: <description>".
        Returns:
            str: The list, or that there are no skills.
        """
        files = sorted(self.folder.glob("*/SKILL.md")) if self.folder.is_dir() else []
        lines = []
        for file in files:
            fields, _ = self.header(file.read_text(encoding="utf-8"))
            lines.append(f"- {file.parent.name}: {fields.get('description', '(no description)')}")
        return "Skills (read one with name):\n" + "\n".join(lines) if lines else "There are no skills."

    def read(self, name: str) -> str:
        """
        A skill's procedure.
        Args:
            name: The skill's name (its folder).
        Returns:
            str: The skill's body, after its header.
        Raises:
            ValueError: If the name is not a plain name, or no such skill exists.
        """
        name = name.strip()
        if not self.NAME.match(name):
            raise ValueError(f"'{name}' is not a skill name; names use letters, digits, - and _.")
        file = self.folder / name / "SKILL.md"
        if not file.is_file():
            raise ValueError(f"There is no skill '{name}'.\n{self.list()}")
        _, body = self.header(file.read_text(encoding="utf-8"))
        return body

    @classmethod
    def build_parser(cls) -> ToolArgumentParser:
        """
        The command line: "[--name=<skill>]".
        Returns:
            ToolArgumentParser: The parser.
        """
        parser = ToolArgumentParser("skill", cls.VERSION, "Read one of the agents' skills, or list them.")
        parser.add_argument("--name", help="The skill to read; omit it for the list")
        return parser

    def run(self, args: argparse.Namespace) -> str:
        """
        Read the skill a command line names, or list the skills.
        Args:
            args: The command line, from `build_parser`.
        Returns:
            str: The skill, or the list.
        """
        return self.read(args.name) if args.name else self.list()


def main(argv: Optional[list[str]] = None) -> int:
    """
    Parse the command line, read the skill and print it, or "Error: <reason>".
    Args:
        argv: Arguments; None reads sys.argv.
    Returns:
        int: 0 on success, 1 on an error.
    """
    try:
        args = Skills.build_parser().parse_args(argv)
        print(Skills().run(args), end="")
    except (ValueError, OSError) as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
