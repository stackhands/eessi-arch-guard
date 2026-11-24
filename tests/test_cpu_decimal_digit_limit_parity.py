"""A run of decimal digits longer than the interpreter's limit is not an error.

Python 3.11 and later make int() raise ValueError for more than
sys.get_int_max_str_digits() digits, leading zeros included. Older
interpreters have no such limit, and the cases use 5000 digits there. The guard read
the processor numbers of a cpu list, a range, and a cpuinfo processor line
with int(), so a list or a line with that many leading zeros ended the guard
in a failed decision, where the Zig guard reads the number and goes on. The
Python guard is checked alone, in process and as a program, and the two
programs are compared through the differential runner, each on a site of its
own. The comparison skips when TOOL_BIN is unset.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from typing import Mapping, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import differential  # noqa: E402
import eessi_arch_guard as guard  # noqa: E402
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


def _digits() -> int:
    """One digit past the interpreter's limit, or 5000 where it has none."""
    get_limit = getattr(sys, "get_int_max_str_digits", None)
    limit = get_limit() if get_limit is not None else 0
    return limit + 1 if limit else 5000


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

    def _cases(self) -> Mapping[str, Tuple[str, str]]:
        zeros = "0" * _digits()
        return {
            "list": (_cpuinfo(), zeros + "1"),
            "range": (_cpuinfo(), "1-" + zeros + "1"),
            "processor line": (_cpuinfo("processor\t: " + zeros + "1"), "1"),
            "past the cap": (_cpuinfo(), "1" + zeros),
        }

    def test_the_cpu_spec_reads_a_long_run_of_digits(self) -> None:
        zeros = "0" * _digits()
        self.assertEqual(guard.parse_cpu_spec(zeros + "1"), frozenset({1}))
        self.assertEqual(guard.parse_cpu_spec("1-" + zeros + "1"), frozenset({1}))
        self.assertEqual(guard.parse_cpu_spec("0," + zeros + "3"), frozenset({0, 3}))
        self.assertIsNone(guard.parse_cpu_spec("1" + zeros))
        self.assertIsNone(guard.parse_cpu_spec("0-1" + zeros))

    def test_a_processor_line_with_many_digits_is_read(self) -> None:
        text = "processor : %s1\nflags : a b\nprocessor : 2\nflags : a\n" % ("0" * _digits())
        scoped = guard.scope_cpuinfo(text, frozenset({1}))
        self.assertEqual(scoped, "processor : 1\nflags : a b\n")

    def test_the_python_program_decides_on_each_case(self) -> None:
        for name, (text, bind) in self._cases().items():
            with self.subTest(case=name):
                run = differential.run_fresh(
                    differential.python_guard(), _build(text, bind), ["--json"]
                )
                self.assertEqual(run.returncode, 0, run.stderr)
                decision = json.loads(run.stdout)
                self.assertNotEqual(decision["decision"], "failed", decision["message"])
                if name != "past the cap":
                    self.assertEqual(decision["now"], "x86_64/intel/icelake")

    def test_programs_agree_on_each_case(self) -> None:
        harness.skip_unless_tool_bin(self)
        for name, (text, bind) in self._cases().items():
            with self.subTest(case=name):
                differential.assert_agree(self, _build(text, bind), ["--json"])


if __name__ == "__main__":
    unittest.main()
