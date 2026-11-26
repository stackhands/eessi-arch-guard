"""A cpuinfo splits into lines at every line break character.

When a task may use only some of the node's processors, the guard splits the
cpuinfo at a carriage return, a vertical tab, a form feed, the separators
0x1c to 0x1e, NEL, and the Unicode line and paragraph separators as well as
at a newline, and writes the scoped copy with newlines. The CPU detection
reads that copy line by line, so a line break that is not a newline moves the
text after it to a line of its own. The cases put each such break where a
newline stood before the flags line, and use a file with carriage returns
and newlines. The detected target must stay the one of the plain file, and
``golden/cpuinfo-line-breaks.txt`` pins the whole output of each case.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import retire_b_support as support  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cpuinfo"
CPUS = ("x86_64/intel/icelake", "x86_64/generic")
LOADED = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0
BREAKS = {
    "CR": "\r",
    "VT": "\x0b",
    "FF": "\x0c",
    "FS": "\x1c",
    "GS": "\x1d",
    "RS": "\x1e",
    "NEL": "\u0085",
    "LS": "\u2028",
    "PS": "\u2029",
}


class CpuinfoLineBreakTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (*CPUS, LOADED):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.text = (FIXTURE / "emeraldrapids-kvm-no-waitpkg.cpuinfo").read_text(encoding="utf-8")

    def _env(self) -> dict:
        env = harness.base_env(self.site, LOADED, ["R/4.4.2"], gpu="none")
        env["SLURM_CPU_BIND_LIST"] = "0,1"
        return env

    def _decide(self, text: str):
        with open(self.site.cpuinfo, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        argv = [*harness.GUARD_ARGV, "--json"]
        return harness.run_with_timeout(argv, env=self._env(), timeout=RUN_TIMEOUT)

    def _variants(self):
        for name, sep in BREAKS.items():
            yield "before flags: " + name, self.text.replace("\nflags", sep + "flags")
        yield "CRLF", self.text.replace("\n", "\r\n")

    def test_the_cpuinfo_splits_at_every_break(self) -> None:
        plain = json.loads(self._decide(self.text).stdout)
        self.assertEqual(plain["now"], "x86_64/intel/icelake")
        for label, text in self._variants():
            with self.subTest(label=label):
                report = json.loads(self._decide(text).stdout)
                self.assertEqual(report["now"], "x86_64/intel/icelake")

    def test_output_on_the_scoped_copy(self) -> None:
        sheet = support.sheet(self, "cpuinfo-line-breaks")
        for label, text in self._variants():
            with self.subTest(label=label):
                proc = self._decide(text)
                sheet.check(
                    self, support.label(label), support.render_proc(proc, support.site_work(self.site))
                )


if __name__ == "__main__":
    unittest.main()
