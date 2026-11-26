"""The Python and Zig guards must read --timeout alike when it holds
Unicode decimal digits.

Python reads the value with float(), which accepts any character of the
Unicode category Nd as a digit: Arabic-Indic, fullwidth and the rest. The
guard then prints the resulting timeout in its dry-run report. The programs
are compared on exit status, standard output, and standard error for such
values in the two spellings of the option, and for values float() refuses.
The comparison skips when TOOL_BIN is unset.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

CPU = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0
VALUES = {
    "\u0663": 3.0,
    "\uff15": 5.0,
    "\u0663.\u0665": 3.5,
    "\uff15.\uff15": 5.5,
    "1_\u0660": 10.0,
}
# Digits that are not decimal (superscript, circled), and a decimal digit
# with a letter after it: float() refuses each.
REFUSED = ("\u00b2", "\u2460", "\u0663x")


class TimeoutUnicodeDigitsParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        self.site.add_cpu(CPU)
        self.site.add_module(CPU, "R/4.4.2")
        self.site.write_cpuinfo("zen4")

    def _run(self, guard, argv: Sequence[str]):
        env = harness.base_env(self.site, CPU, ["R/4.4.2"], gpu="none")
        return harness.run_with_timeout([*guard, *argv], env=env, timeout=RUN_TIMEOUT)

    def _spellings(self, value: str):
        yield ("--timeout", value, "--dry-run", "--json")
        yield ("--timeout=" + value, "--dry-run", "--json")
        yield ("--timeout", value, "--dry-run")

    def test_python_guard_reads_the_digits(self) -> None:
        guard = [sys.executable, "-I", str(harness.GUARD)]
        for value, seconds in VALUES.items():
            with self.subTest(value=value):
                proc = self._run(guard, ("--timeout", value, "--dry-run", "--json"))
                self.assertEqual(json.loads(proc.stdout)["timeout"], seconds)
                self.assertEqual(proc.stderr, "")

    def test_programs_agree_on_unicode_digits(self) -> None:
        harness.skip_unless_tool_bin(self)
        for value in (*VALUES, *REFUSED):
            for argv in self._spellings(value):
                with self.subTest(argv=" ".join(argv)):
                    python = self._run([sys.executable, "-I", str(harness.GUARD)], argv)
                    program = self._run([str(harness.PROGRAM)], argv)
                    self.assertEqual(program.returncode, python.returncode, program.stderr)
                    self.assertEqual(program.stderr, python.stderr)
                    self.assertEqual(program.stdout, python.stdout)


if __name__ == "__main__":
    unittest.main()
