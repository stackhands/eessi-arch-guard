"""The Lmod site package warns when a module file's software subdirectory disagrees."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SitePackageTests(unittest.TestCase):
    def test_a_mismatch_warns_and_a_match_does_not(self):
        proc = subprocess.run(
            ["lua", str(ROOT / "tests" / "site_package_check.lua"), str(ROOT)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("ok", proc.stdout)
