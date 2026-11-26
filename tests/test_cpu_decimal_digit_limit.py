"""A run of decimal digits of any length is read, not refused.

A cpu list, a range, and a cpuinfo processor line may hold a number with
thousands of leading zeros. The binary reads the number and goes on, where a
reader that refuses long digit strings would end in a failed decision. A
number past the CPU limit names no processor, so a list that holds one is not
a CPU set and is ignored.

Each case runs on a cpuinfo whose processor 0 lacks the flags of the others,
so a bind list read as processors of the others makes the binary choose an
Icelake directory. The cases pin the decision and the whole output in
tests/golden/cpu-decimal-digit-limit.txt.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from typing import Mapping, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import golden  # noqa: E402
import harness  # noqa: E402

LOADED = "x86_64/amd/zen4"
CPUS = ("x86_64/intel/icelake", "x86_64/generic", LOADED)
FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "cpuinfo"
    / "emeraldrapids-kvm-no-waitpkg.cpuinfo"
)
POOR_FLAGS = "flags\t\t: fpu vme de pse tsc msr pae"


DIGITS = 5000


def _cpuinfo(second_header: str = "processor\t: 1") -> str:
    """The fixture with a processor 0 that lacks the flags of the others."""
    blocks = [
        part for part in re.split(r"(?m)^(?=processor\t: )", FIXTURE.read_text()) if part.strip()
    ]
    first = re.sub(r"(?m)^flags\t\t: .*$", POOR_FLAGS, blocks[0])
    second = re.sub(r"(?m)^processor\t: 1$", second_header, blocks[1], count=1)
    return first + second + "".join(blocks[2:])


def _build(text: str, bind: str) -> differential.Build:
    def build(site: harness.Site) -> Mapping[str, str]:
        for cpu in CPUS:
            site.add_cpu(cpu)
            site.add_module(cpu, "R/4.4.2")
        Path(site.cpuinfo).write_text(text, encoding="utf-8")
        env = harness.base_env(site, LOADED, ["R/4.4.2"], gpu="none")
        env["SLURM_CPU_BIND_LIST"] = bind
        return env

    return build


class CpuDecimalDigitLimitTests(unittest.TestCase):
    def _cases(self) -> Mapping[str, Tuple[str, str, bool]]:
        """Each case: cpuinfo, bind list, and whether the task is bound to processor 1 on."""
        zeros = "0" * DIGITS
        return {
            "list": (_cpuinfo(), zeros + "1", True),
            "list after a short number": (_cpuinfo(), "1," + zeros + "3", True),
            "range": (_cpuinfo(), "1-" + zeros + "1", True),
            "range up to a long number": (_cpuinfo(), "1-" + zeros + "3", True),
            "processor line": (_cpuinfo("processor\t: " + zeros + "1"), "1", True),
            "past the cap": (_cpuinfo(), "1" + zeros, False),
            "range past the cap": (_cpuinfo(), "0-1" + zeros, False),
        }

    def test_the_binary_decides_on_each_case(self) -> None:
        sheet = golden.Sheet("cpu-decimal-digit-limit")
        self.addCleanup(sheet.finish)
        for name, (text, bind, reads) in self._cases().items():
            with self.subTest(case=name):
                run = differential.assert_golden(self, sheet, name, _build(text, bind), ["--json"])
                self.assertEqual(run.returncode, 0, run.stderr)
                decision = json.loads(run.stdout)
                self.assertNotEqual(decision["decision"], "failed", decision["message"])
                if reads:
                    self.assertEqual(decision["now"], "x86_64/intel/icelake")
                else:
                    self.assertNotEqual(decision["now"], "x86_64/intel/icelake")


if __name__ == "__main__":
    unittest.main()
