"""Black-box CLI precedence checks for help and version with restore."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "src" / "eessi_arch_guard.py"


def run_guard(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(GUARD), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


class HelpVersionRestorePrecedenceTests(unittest.TestCase):
    def test_restore_without_stdin_reports_parse_error(self) -> None:
        proc = run_guard("--restore")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("restore failed:", proc.stderr)

    def test_help_should_override_restore(self) -> None:
        proc = run_guard("--help", "--restore")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("usage: eessi-arch-guard", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_version_should_override_restore(self) -> None:
        proc = run_guard("--version", "--restore")
        self.assertEqual(proc.returncode, 0)
        self.assertRegex(proc.stdout, r"^\d+\.\d+\.\d+\n\Z")
        self.assertEqual(proc.stderr, "")


if __name__ == "__main__":
    unittest.main()
