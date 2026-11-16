"""The docs and sources say which question they answer in words."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTE_ID = re.compile(r"\b(?:open|ns)-\d{3}\b|\bappendix [A-Z]\b")


def note_ids() -> list:
    """Lines that cite a design-note id such as open-003 or appendix A."""
    paths = [ROOT / name for name in ("README.md", "DESIGN.md", "COMPAT.md", "CHANGELOG.md")]
    for sub, pattern in (("docs", "*.md"), ("src", "*.py"), ("zig", "*.zig"), ("tests", "*.py")):
        paths.extend(sorted((ROOT / sub).rglob(pattern)))
    found = []
    for path in paths:
        if path == Path(__file__).resolve() or "_build" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if NOTE_ID.search(line):
                found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    return found


class DocsTests(unittest.TestCase):
    def test_no_design_note_ids(self):
        self.assertEqual(note_ids(), [])


if __name__ == "__main__":
    unittest.main()
