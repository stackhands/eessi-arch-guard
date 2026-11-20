"""The Python and Zig guards must treat padding around a timeout alike.

``float`` skips ASCII whitespace and whitespace outside ASCII, but not the
ASCII separators U+001C to U+001F, which ``str.strip`` removes. A timeout
padded with one of them is invalid for the Python guard. The cases run both
programs on one generated tree and compare exit status, standard output, and
standard error. The comparison skips when TOOL_BIN is unset.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0

ACCEPTED = {
    "5": 5.0,
    " 5 ": 5.0,
    "\t5\n": 5.0,
    "\x0b5\x0c": 5.0,
    "\r5": 5.0,
    "\u00a05\u00a0": 5.0,
    "\u20285\u2029": 5.0,
    "\u30005": 5.0,
}
REJECTED = (
    "\x1c5",
    "5\x1c",
    "\x1d5",
    "5\x1e",
    "\x1f5",
    " \x1c5",
    "5\x1f ",
    "\x1c \u00a05",
    "5\u00a0\x1d",
    "\x1c",
    "\x1f",
    "\x1c\x1d\x1e\x1f",
)
TIMEOUTS = (*ACCEPTED, *REJECTED)
DEFAULT_TIMEOUT = 60.0


class TimeoutPaddingParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.site.write_cpuinfo("zen2")
        self.env: Dict[str, str] = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")

    def _python(self, text: str):
        argv = [sys.executable, "-I", str(harness.GUARD), "--dry-run", "--json"]
        argv += ["--timeout", text]
        return harness.run_with_timeout(argv, env=self.env, timeout=RUN_TIMEOUT)

    def _program(self, text: str):
        argv = [str(harness.PROGRAM), "--dry-run", "--json", "--timeout", text]
        return harness.run_with_timeout(argv, env=self.env, timeout=RUN_TIMEOUT)

    def test_python_guard_rejects_separator_padding(self) -> None:
        for text in TIMEOUTS:
            with self.subTest(timeout=text):
                proc = self._python(text)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                doc = json.loads(proc.stdout)
                if text in ACCEPTED:
                    self.assertEqual(doc["timeout"], ACCEPTED[text])
                    self.assertEqual(proc.stderr, "")
                else:
                    self.assertEqual(doc["timeout"], DEFAULT_TIMEOUT)
                    self.assertIn("--timeout is not a positive number", proc.stderr)

    def test_programs_agree_on_padded_timeouts(self) -> None:
        harness.skip_unless_tool_bin(self)
        for text in TIMEOUTS:
            python = self._python(text)
            program = self._program(text)
            with self.subTest(timeout=text):
                self.assertEqual(program.returncode, python.returncode)
                self.assertEqual(program.stderr, python.stderr)
                self.assertEqual(program.stdout, python.stdout)


if __name__ == "__main__":
    unittest.main()
