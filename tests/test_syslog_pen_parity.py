"""The Python and Zig guards must accept the same syslog enterprise numbers.

The Python guard takes any positive decimal number of up to twenty digits.
A twenty-digit number can exceed the largest value a 64-bit integer holds, so
a parser that stores it in one rejects numbers the Python guard accepts. The
cases run both programs on one generated tree and compare exit status, standard
output, and standard error. The comparison skips when TOOL_BIN is unset.
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
ARGS = ("--dry-run", "--json")

PENS = {
    "99999999999999999999": "99999999999999999999",
    "18446744073709551616": "18446744073709551616",
    "18446744073709551615": "18446744073709551615",
    "00000000000000000001": "1",
    "  18446744073709551616  ": "18446744073709551616",
    "100000000000000000000": "32473",
    "0": "32473",
}


class SyslogPenParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.site.write_cpuinfo("zen2")

    def _env(self, pen: str) -> Dict[str, str]:
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        env["EESSI_ARCH_GUARD_SYSLOG_PEN"] = pen
        return env

    def _python(self, env: Dict[str, str]):
        argv = [sys.executable, "-I", str(harness.GUARD), *ARGS]
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT)

    def test_python_guard_reports_the_number_it_accepts(self) -> None:
        for pen, shown in PENS.items():
            with self.subTest(pen=pen):
                proc = self._python(self._env(pen))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                doc = json.loads(proc.stdout)
                self.assertEqual(doc["syslog"]["pen"], shown)

    def test_programs_agree_on_enterprise_numbers(self) -> None:
        harness.skip_unless_tool_bin(self)
        for pen in PENS:
            env = self._env(pen)
            python = self._python(env)
            program = harness.run_with_timeout(
                [str(harness.PROGRAM), *ARGS], env=env, timeout=RUN_TIMEOUT
            )
            with self.subTest(pen=pen):
                self.assertEqual(program.returncode, python.returncode)
                self.assertEqual(program.stderr, python.stderr)
                self.assertEqual(program.stdout, python.stdout)


if __name__ == "__main__":
    unittest.main()
