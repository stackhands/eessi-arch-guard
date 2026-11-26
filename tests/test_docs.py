"""The docs and sources say which question they answer in words."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DocsTests(unittest.TestCase):
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
