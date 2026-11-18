"""The CVMFS client revision, compared only when a site asks."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    path = ROOT / "share" / "cvmfsrev.py"
    spec = importlib.util.spec_from_file_location("cvmfsrev", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RevisionTests(unittest.TestCase):
    def test_stat_output_names_one_revision(self):
        rev = load()
        self.assertEqual(rev.revision_of("Revision: 42\nExpires in: 4 minutes\n"), 42)
        self.assertEqual(rev.revision_of("Catalog revision: 7\n"), 7)
        self.assertEqual(rev.revision_of("Repository revision: 9\n"), 9)
        self.assertIsNone(rev.revision_of("Connected via proxy DIRECT\n"))

    def test_without_a_minimum_it_reports_and_does_not_fail(self):
        rev = load()
        code, text = rev.gate(42, None)
        self.assertEqual(code, 0)
        self.assertEqual(text, "revision 42\n")
        code, text = rev.gate(42, 40)
        self.assertEqual(code, 0, text)
        code, text = rev.gate(42, 42)
        self.assertEqual(code, 0, text)
        code, text = rev.gate(42, 50)
        self.assertEqual(code, 1)
        self.assertIn("behind", text)
        code, text = rev.gate(None, 50)
        self.assertEqual(code, 2)
        self.assertIn("no revision", text)

    def test_the_how_to_names_the_gate(self):
        page = (ROOT / "docs" / "orgmode" / "howto" / "check-the-client-revision.org").read_text()
        self.assertIn("cvmfsrev.py", page)
        self.assertIn("--min", page)


if __name__ == "__main__":
    unittest.main()
