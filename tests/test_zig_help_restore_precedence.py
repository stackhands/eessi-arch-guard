"""Black-box CLI precedence checks for the Zig build with --restore."""

from __future__ import annotations

import subprocess
import unittest

import harness


def run_guard(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*harness.GUARD_ARGV, *args],
        input=stdin,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


@unittest.skipUnless(harness.TOOL_BIN, "TOOL_BIN is required")
class ZigHelpVersionRestorePrecedenceTests(unittest.TestCase):
    def test_help_overrides_restore(self) -> None:
        proc = run_guard("--help", "--restore")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage: eessi-arch-guard", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_version_overrides_restore(self) -> None:
        proc = run_guard("--version", "--restore")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stdout, r"^\d+\.\d+\.\d+ \(.*\)\n\Z")
        self.assertEqual(proc.stderr, "")


if __name__ == "__main__":
    unittest.main()
