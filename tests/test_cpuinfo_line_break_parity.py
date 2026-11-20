"""The Python and Zig guards must split a cpuinfo into lines alike.

When a task may use only some of the node's processors, the guard reads the
cpuinfo with str.splitlines, which ends a line at a carriage return, a
vertical tab, a form feed, the separators 0x1c to 0x1e, NEL, and the Unicode
line and paragraph separators as well as at a newline, and writes the
scoped copy with newlines. The CPU detection reads that copy line by line, so
a line break that is not a newline moves the text after it to a line of its
own. The cases put each such break where a newline stood before the flags
line, and use a file with carriage returns and newlines, and compare the
detected target of both programs. The comparison skips when TOOL_BIN is
unset.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

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


class CpuinfoLineBreakParityTests(unittest.TestCase):
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

    def _decide(self, guard, text: str):
        with open(self.site.cpuinfo, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return harness.run_with_timeout([*guard, "--json"], env=self._env(), timeout=RUN_TIMEOUT)

    def _variants(self):
        for name, sep in BREAKS.items():
            yield "before flags: " + name, self.text.replace("\nflags", sep + "flags")
        yield "CRLF", self.text.replace("\n", "\r\n")

    def test_python_guard_splits_at_every_break(self) -> None:
        guard = [sys.executable, "-I", str(harness.GUARD)]
        plain = json.loads(self._decide(guard, self.text).stdout)
        self.assertEqual(plain["now"], "x86_64/intel/icelake")
        for label, text in self._variants():
            with self.subTest(label=label):
                report = json.loads(self._decide(guard, text).stdout)
                self.assertEqual(report["now"], "x86_64/intel/icelake")

    def test_programs_agree_on_the_scoped_copy(self) -> None:
        harness.skip_unless_tool_bin(self)
        for label, text in self._variants():
            with self.subTest(label=label):
                python = self._decide([sys.executable, "-I", str(harness.GUARD)], text)
                program = self._decide([str(harness.PROGRAM)], text)
                self.assertEqual(program.returncode, python.returncode, program.stderr)
                self.assertEqual(program.stderr, python.stderr)
                self.assertEqual(program.stdout, python.stdout)


if __name__ == "__main__":
    unittest.main()
