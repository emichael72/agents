"""
Offline tests for the skill tool: a temporary skills folder, and the skills the repository ships.
Run from the repository root:
    .venv/bin/python -m unittest discover -s tools/skill/tests
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from skill import Skills, main  # noqa: E402


class SkillToolTests(unittest.TestCase):
    """Listing and reading skills, and refusing names that are not skills."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        (self.folder / "build").mkdir()
        (self.folder / "build" / "SKILL.md").write_text(
            "---\nname: build\ndescription: Build and test a project.\n---\n\n# Build\n\n---\n\n1. Run make.\n")
        (self.folder / "notes").mkdir()  # No SKILL.md: not a skill
        self.skills = Skills(self.folder)

    def test_list_names_each_skill_with_its_description(self):
        self.assertEqual(self.skills.list(), "Skills (read one with name):\n- build: Build and test a project.")
        self.assertEqual(Skills(self.folder / "missing").list(), "There are no skills.")

    def test_read_returns_the_procedure_without_its_header(self):
        self.assertEqual(self.skills.read("build"), "# Build\n\n---\n\n1. Run make.\n")  # A later --- is kept

    def test_read_refuses_what_is_not_a_skill(self):
        for name in ("../build", "build/SKILL.md", "", "-x"):
            with self.assertRaisesRegex(ValueError, "is not a skill name"):
                self.skills.read(name)
        with self.assertRaisesRegex(ValueError, "There is no skill 'notes'(.|\n)*- build:"):
            self.skills.read("notes")

    def test_the_shipped_pull_request_skill(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--name=pull-request"]), 0)
        self.assertTrue(out.getvalue().startswith("# Change code and open a pull request"))
        self.assertIn("pr tool, action check", out.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--name=../context"]), 1)


if __name__ == "__main__":
    unittest.main()
