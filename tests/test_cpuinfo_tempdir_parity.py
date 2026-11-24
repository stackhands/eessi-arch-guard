"""The Python and Zig guards must treat the scoped cpuinfo copy alike.

When a task may use only some of the node's processors, the guard writes the
cpuinfo of those processors to a temporary file for the CPU detection. The
file goes in the directory tempfile.gettempdir chooses, which is found by
creating and writing a probe file in each candidate directory. When none of
them takes a file, Python raises FileNotFoundError from that call and the
guard reports "skipped" with the text. When the probe passes and the copy is
written short, Python goes on with the short file, because os.write is not
looped.

The cases make a real write fail without privileges: each program runs on a
fresh site under the shared size limiter, so the kernel answers with EFBIG,
or with a short write, instead of a signal. The programs are compared on
exit status, standard output, and standard error in each output mode. The
comparison skips when TOOL_BIN is unset.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from typing import Dict, Sequence
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import harness  # noqa: E402

CPU = "x86_64/intel/sapphirerapids"
FALLBACK = "x86_64/intel/icelake"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cpuinfo"
MODES: Sequence[Sequence[str]] = (
    (),
    ("--json",),
    ("--dry-run",),
    ("--dry-run", "--json"),
    ("--shell",),
    ("--strict",),
    ("--strict", "--json"),
)
SKIP_TEXT = "No usable temporary directory found in ["


def build(site: harness.Site) -> Dict[str, str]:
    for cpu in (CPU, FALLBACK):
        site.add_cpu(cpu)
        site.add_module(cpu, "R/4.4.2")
    text = (FIXTURE / "emeraldrapids-kvm-no-waitpkg.cpuinfo").read_text(encoding="utf-8")
    text, visible = harness.on_task_cpus(text, 2)
    Path(site.cpuinfo).write_text(text, encoding="utf-8")
    env = harness.base_env(site, CPU, ["R/4.4.2"], gpu="none")
    env["SLURM_CPU_BIND_LIST"] = ",".join(str(c) for c in visible)
    return env


class CpuinfoTempdirParityTests(unittest.TestCase):
    def _compare(self, limit: int) -> None:
        for mode in MODES:
            with self.subTest(limit=limit, mode=" ".join(mode)):
                differential.assert_agree(self, build, mode, limit)

    def test_python_guard_skips_when_no_directory_takes_a_file(self) -> None:
        for mode in MODES:
            with self.subTest(mode=" ".join(mode)):
                run = differential.run_fresh(differential.python_guard(), build, mode, 0)
                text = run.stdout + run.stderr
                self.assertIn(SKIP_TEXT, text)
                self.assertIn("FileNotFoundError", text)

    def test_programs_agree_when_no_directory_takes_a_file(self) -> None:
        harness.skip_unless_tool_bin(self)
        self._compare(0)

    def test_programs_agree_when_the_copy_is_written_short(self) -> None:
        harness.skip_unless_tool_bin(self)
        self._compare(4)
        self._compare(300)


class OnTaskCpusTests(unittest.TestCase):
    """harness.on_task_cpus, under affinity masks the host may not have."""

    def _renumber(self, mask, visible: int):
        text = (FIXTURE / "emeraldrapids-kvm-no-waitpkg.cpuinfo").read_text(encoding="utf-8")
        with mock.patch.object(harness.os, "sched_getaffinity", return_value=set(mask)):
            got, bound = harness.on_task_cpus(text, visible)
        ids = [int(n) for n in re.findall(r"^processor[ \t]*:[ \t]*([0-9]+)", got, re.M)]
        return ids, bound

    def test_a_mask_of_only_high_ids_binds_processors_the_fixture_has(self) -> None:
        ids, bound = self._renumber({8, 9}, 2)
        self.assertEqual(bound, [8, 9])
        self.assertEqual(ids, [8, 9, 10, 11, 12, 13, 14, 15])

    def test_a_mask_with_a_gap_keeps_its_ids_in_order(self) -> None:
        ids, bound = self._renumber({3, 17, 40}, 2)
        self.assertEqual(bound, [3, 17])
        self.assertEqual(ids, [3, 17, 40, 41, 42, 43, 44, 45])

    def test_a_mask_of_zero_to_seven_keeps_the_fixture_numbers(self) -> None:
        ids, bound = self._renumber(range(64), 2)
        self.assertEqual(bound, [0, 1])
        self.assertEqual(ids, list(range(8)))

    def test_a_mask_too_small_for_the_bind_list_skips(self) -> None:
        with self.assertRaises(unittest.SkipTest):
            self._renumber({12}, 2)


if __name__ == "__main__":
    unittest.main()
