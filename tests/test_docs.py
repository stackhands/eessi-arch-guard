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

    def test_configuration_doc_matches_dangling_symlink_behaviour(self):
        cfg = (ROOT / "docs" / "orgmode" / "reference" / "configuration.org").read_text(
            encoding="utf-8"
        )
        compat = (ROOT / "COMPAT.md").read_text(encoding="utf-8")
        self.assertNotIn("points nowhere counts as no file", cfg)
        self.assertNotIn("dangling symlink counts as absent", compat)

    def test_environment_doc_lists_every_guard_env_var(self):
        source = "".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "zig").glob("*.zig"))
            if path.name != "pylower_table.zig"
        )
        env_doc = (ROOT / "docs" / "orgmode" / "reference" / "environment.org").read_text(
            encoding="utf-8"
        )
        msgids = {"EESSI_ARCH_RELOAD", "EESSI_ARCH_FAIL", "EESSI_ARCH_SKIP", "EESSI_ARCH_NOOP"}
        ignored = msgids | {"SLURM_X"}
        names = sorted(
            name
            for name in set(re.findall(r'"((?:EESSI|XDG|SLURM)_[A-Z0-9_]+)"', source))
            if name not in ignored
        )
        missing = [name for name in names if name not in env_doc]
        self.assertEqual(missing, [])

    def test_the_site_tool_how_tos_name_the_commands(self):
        mount = (ROOT / "docs" / "orgmode" / "howto" / "check-the-client-mount.org").read_text()
        self.assertIn("--check-mount", mount)
        self.assertIn("site modules", mount)
        self.assertIn("exit status", mount.lower())
        revision = (
            ROOT / "docs" / "orgmode" / "howto" / "check-the-client-revision.org"
        ).read_text()
        self.assertIn("--check-revision", revision)
        self.assertIn("--min", revision)


if __name__ == "__main__":
    unittest.main()
