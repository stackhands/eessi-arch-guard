"""--timeout accepts Unicode decimal digits.

Any character of the Unicode category Nd counts as a digit in the value:
Arabic-Indic, fullwidth and the rest. The guard prints the resulting timeout
in its dry-run report. The first test checks the value of each spelling, and
``golden/timeout-unicode-digits.txt`` pins the exit status and both streams
for such values in the two spellings of the option, and for values that are
refused (superscript and circled digits, a digit followed by a letter).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import retire_b_support as support  # noqa: E402

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


class TimeoutUnicodeDigitsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        self.site.add_cpu(CPU)
        self.site.add_module(CPU, "R/4.4.2")
        self.site.write_cpuinfo("zen4")

    def _run(self, argv: Sequence[str]):
        env = harness.base_env(self.site, CPU, ["R/4.4.2"], gpu="none")
        return harness.run_with_timeout([*harness.GUARD_ARGV, *argv], env=env, timeout=RUN_TIMEOUT)

    def _spellings(self, value: str):
        yield ("--timeout", value, "--dry-run", "--json")
        yield ("--timeout=" + value, "--dry-run", "--json")
        yield ("--timeout", value, "--dry-run")

    def test_the_digits_are_read(self) -> None:
        for value, seconds in VALUES.items():
            with self.subTest(value=value):
                proc = self._run(("--timeout", value, "--dry-run", "--json"))
                self.assertEqual(json.loads(proc.stdout)["timeout"], seconds)
                self.assertEqual(proc.stderr, "")

    def test_output_for_unicode_digits(self) -> None:
        sheet = support.sheet(self, "timeout-unicode-digits")
        for value in (*VALUES, *REFUSED):
            for argv in self._spellings(value):
                label = support.label(" ".join(argv))
                with self.subTest(argv=label):
                    proc = self._run(argv)
                    support.check(self, sheet, label, proc, self.site)


if __name__ == "__main__":
    unittest.main()
