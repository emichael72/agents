"""
Offline tests for the ed tool's matching: differences a model's copy often has are evened out, a text
that is not found points at the closest lines, and an insert can name the line it goes next to.
Run from the repository root:
    .venv/bin/python -m unittest discover -s tools/ed/tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from gatekeepers.fs.fs_gate import FsGate
from tools.ed.ed import Editor

README = ("# Core Dump\n\nBecause code review asks, “Does this work?”\n\nWe also ask – “Can you explain why?”\n\n"
          "## Usage\n\n```sh\n./core_dump --date\n./core_dump --pi\n```\n\n## Layout\n\nsrc/main.c    Argument parsing\n")


class EditorMatchingTests(unittest.TestCase):
    """The edits a small model got wrong in real runs, against a README like core_dump's."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        paths = self.folder / "paths.json"
        paths.write_text(json.dumps({"paths": {"proj": {"path": str(self.folder), "access": "rw"}}}))
        self.editor = Editor(FsGate.load(paths))
        self.file = self.folder / "README.md"
        self.file.write_text(README)

    def test_straight_quotes_and_a_hyphen_match_the_files_curly_ones_and_dash(self):
        out = self.editor.edit("proj/README.md", "replace",
                               'We also ask - "Can you explain why?"', "We also ask: why does it work?")
        self.assertIn("replaced 1 occurrence, matched with quotes and dashes evened out", out)
        self.assertEqual(self.file.read_text(), README.replace("We also ask – “Can you explain why?”",
                                                               "We also ask: why does it work?"))

    def test_different_spacing_matches(self):
        out = self.editor.edit("proj/README.md", "replace", "src/main.c Argument parsing", "src/main.c    Options")
        self.assertIn("matched with spacing evened out", out)
        self.assertTrue(self.file.read_text().endswith("src/main.c    Options\n"))

    def test_misremembered_text_is_refused_with_the_closest_lines(self):
        with self.assertRaises(ValueError) as refused:  # The model forgot --pi and invented --help
            self.editor.edit("proj/README.md", "replace", "./core_dump --date\n./core_dump --help\n```", "x")
        message = str(refused.exception)
        self.assertIn("not even with spacing, quotes and dashes evened out. The closest text is lines 10-12", message)
        self.assertIn("    11  ./core_dump --pi", message)  # The real line, numbered, to copy from
        self.assertEqual(self.file.read_text(), README)  # Nothing changed

    def test_an_evened_out_match_in_two_places_is_refused(self):
        self.file.write_text("say “hi”\nsay “hi”\n")
        with self.assertRaisesRegex(ValueError, r"matches 2 places in 'proj/README.md' \(lines 1, 2\)"):
            self.editor.edit("proj/README.md", "replace", 'say "hi"', "x")

    def test_insert_after_or_before_a_lines_text(self):
        self.editor.edit("proj/README.md", "insert", new="## Prime\n\nFactors.\n", before="## Layout")
        self.editor.edit("proj/README.md", "insert", new="./core_dump --prime 60", after="./core_dump --pi")
        text = self.file.read_text()
        self.assertIn("./core_dump --pi\n./core_dump --prime 60\n```", text)
        self.assertIn("## Prime\n\nFactors.\n## Layout", text)
        with self.assertRaisesRegex(ValueError, r"3 lines of 'proj/README.md' hold '##' \(lines 7, 15, 18\)"):
            self.editor.edit("proj/README.md", "insert", new="x", after="## ")
        with self.assertRaisesRegex(ValueError, "No line of 'proj/README.md' holds '## Licence'"):
            self.editor.edit("proj/README.md", "insert", new="x", after="## Licence")


if __name__ == "__main__":
    unittest.main()
