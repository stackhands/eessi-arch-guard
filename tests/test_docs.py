"""The docs and sources say which question they answer in words."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DocsTests(unittest.TestCase):
    def test_environment_doc_lists_every_guard_env_var(self):
        source = (ROOT / "src" / "eessi_arch_guard.py").read_text(encoding="utf-8")
        env_doc = (ROOT / "docs" / "orgmode" / "reference" / "environment.org").read_text(
            encoding="utf-8"
        )
        msgids = {"EESSI_ARCH_RELOAD", "EESSI_ARCH_FAIL", "EESSI_ARCH_SKIP", "EESSI_ARCH_NOOP"}
        ignored = msgids | {"SLURM_X"}
        names = sorted(
            name
            for name in set(re.findall(r'["\']((?:EESSI|XDG|SLURM)_[A-Z0-9_]+)["\']', source))
            if name not in ignored
        )
        missing = [name for name in names if name not in env_doc]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
