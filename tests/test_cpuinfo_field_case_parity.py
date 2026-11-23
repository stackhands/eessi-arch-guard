"""The Python and Zig guards must tell two cpuinfo field names apart alike.

The guard writes a scoped cpuinfo copy only when the processors the task may
use do not all describe the same CPU. Two processors describe the same CPU
when their fields, taken by lower-cased name, hold the same first values. A
copy that is not needed is not written, so the case where a field is spelled
with another case on another processor decides whether the guard goes on to
the CPU detection or tries to place a copy.

The cases run each program on a fresh site under the shared size limiter at
zero. Placing the copy then fails with the error of tempfile.gettempdir, and
the detection fails on its own file. The cause in the decision says which of
the two happened. A field that two processors spell alike in the lower-cased
name, such as ``key``, ``Key`` and ``KEY``, needs no copy. A field that only
one processor has, or that holds another value, needs one. The programs are
compared on exit status and on both output streams, and the Python guard is
checked for the cause each case should give. The comparison skips when
TOOL_BIN is unset.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Callable, Dict, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import harness  # noqa: E402

CPU = "x86_64/intel/sapphirerapids"
FALLBACK = "x86_64/intel/icelake"
NO_COPY = "archdetect-failed"
COPY_FAILED = "FileNotFoundError"
Case = Tuple[str, Sequence[str], str]


def builder(
    names: Sequence[str], value: Callable[[int], str]
) -> Callable[[harness.Site], Dict[str, str]]:
    """A site whose first processors carry one field each, named by ``names``."""

    def build(site: harness.Site) -> Dict[str, str]:
        for cpu in (CPU, FALLBACK):
            site.add_cpu(cpu)
            site.add_module(cpu, "R/4.4.2")
        visible = sorted(os.sched_getaffinity(0))[: len(names)]
        blocks = []
        for index, (cpu_id, name) in enumerate(zip(visible, names)):
            blocks.append(
                "processor\t: %d\nvendor_id\t: GenuineIntel\n%s\t: %s\nflags\t\t: avx2 fma\n"
                % (cpu_id, name, value(index))
            )
        Path(site.cpuinfo).write_text("\n".join(blocks), encoding="utf-8")
        env = harness.base_env(site, CPU, ["R/4.4.2"], gpu="none")
        env["SLURM_CPU_BIND_LIST"] = ",".join(str(cpu_id) for cpu_id in visible)
        return env

    return build


CASES: Sequence[Case] = (
    ("same name", ("key", "key"), NO_COPY),
    ("title and lower", ("Key", "key"), NO_COPY),
    ("upper and lower", ("KEY", "key"), NO_COPY),
    ("title and upper", ("Key", "KEY"), NO_COPY),
    ("three spellings", ("key", "Key", "KEY"), NO_COPY),
    ("padded name", ("key", "  Key  "), NO_COPY),
    ("other name", ("key", "Kez"), COPY_FAILED),
)


def cause_of(run: differential.Result) -> str:
    return json.loads(run.stdout.splitlines()[0])["cause"]


class CpuinfoFieldCaseParityTests(unittest.TestCase):
    def setUp(self) -> None:
        if len(os.sched_getaffinity(0)) < 3:
            self.skipTest("the cases need three processors in the affinity set")

    def test_python_guard_places_a_copy_only_for_fields_that_differ(self) -> None:
        for label, names, cause in CASES:
            with self.subTest(case=label):
                run = differential.run_fresh(
                    differential.python_guard(), builder(names, lambda _i: "x"), ("--json",), 0
                )
                self.assertEqual(cause_of(run), cause, run.stdout)

    def test_python_guard_places_a_copy_when_the_values_differ(self) -> None:
        run = differential.run_fresh(
            differential.python_guard(),
            builder(("key", "Key"), lambda index: "v%d" % index),
            ("--json",),
            0,
        )
        self.assertEqual(cause_of(run), COPY_FAILED, run.stdout)

    def test_programs_agree_on_fields_spelled_in_another_case(self) -> None:
        harness.skip_unless_tool_bin(self)
        for label, names, _cause in CASES:
            for mode in ((), ("--json",)):
                with self.subTest(case=label, mode=" ".join(mode)):
                    differential.assert_agree(self, builder(names, lambda _i: "x"), mode, 0)

    def test_programs_agree_when_the_values_differ(self) -> None:
        harness.skip_unless_tool_bin(self)
        for mode in ((), ("--json",)):
            with self.subTest(mode=" ".join(mode)):
                differential.assert_agree(
                    self, builder(("key", "Key"), lambda index: "v%d" % index), mode, 0
                )


if __name__ == "__main__":
    unittest.main()
