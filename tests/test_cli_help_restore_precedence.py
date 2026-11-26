"""Black-box CLI precedence checks for help and version with restore."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "src" / "eessi_arch_guard.py"

# program, flag, version regex (None means the help text).
HELP_VERSION_ROWS = (
    ("python", "--help", None),
    ("python", "--version", r"^\d+\.\d+\.\d+\n\Z"),
    ("zig", "--help", None),
    ("zig", "--version", r"^\d+\.\d+\.\d+ \(.*\)\n\Z"),
)


def run_guard(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(GUARD), *args],
        input=stdin,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


class HelpVersionRestorePrecedenceTests(unittest.TestCase):
    def test_restore_without_stdin_reports_parse_error(self) -> None:
        proc = run_guard("--restore")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("restore failed:", proc.stderr)

    def test_help_and_version_override_restore(self) -> None:
        for program, flag, version_re in HELP_VERSION_ROWS:
            with self.subTest(program=program, flag=flag):
                if program == "zig" and not harness.TOOL_BIN:
                    continue
                argv = (
                    [str(harness.PROGRAM), flag, "--restore"]
                    if program == "zig"
                    else [sys.executable, "-I", str(GUARD), flag, "--restore"]
                )
                proc = subprocess.run(
                    argv, capture_output=True, encoding="utf-8", check=False
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                if version_re is None:
                    self.assertIn("usage: eessi-arch-guard", proc.stdout)
                else:
                    self.assertRegex(proc.stdout, version_re)


if __name__ == "__main__":
    unittest.main()
