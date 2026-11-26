"""The scoped cpuinfo copy is written to a temporary directory, or the run reports it.

When a task may use only some of the node's processors, the binary writes the
cpuinfo of those processors to a temporary file for the CPU detection. When no
candidate directory takes a file, the binary reports "skipped" with the text
"No usable temporary directory found in [" and the class FileNotFoundError.
When the probe passes and the copy is written short, the binary goes on with
the short file.

The cases make a real write fail without privileges: each run uses a fresh
site under a file-size limit, so the kernel answers with EFBIG, or with a
short write, instead of a signal. Exit status, standard output, and standard
error of each output mode are pinned in tests/golden/cpuinfo-tempdir-none.txt
and tests/golden/cpuinfo-tempdir-short.txt.
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
import golden  # noqa: E402
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


class CpuinfoTempdirTests(unittest.TestCase):
    def test_the_binary_skips_when_no_directory_takes_a_file(self) -> None:
        sheet = golden.Sheet("cpuinfo-tempdir-none")
        self.addCleanup(sheet.finish)
        for mode in MODES:
            with self.subTest(mode=" ".join(mode)):
                run = differential.assert_golden(
                    self, sheet, " ".join(mode) or "text", build, mode, 0
                )
                text = run.stdout + run.stderr
                self.assertIn(SKIP_TEXT, text)
                self.assertIn("FileNotFoundError", text)

    def test_the_binary_goes_on_when_the_copy_is_written_short(self) -> None:
        sheet = golden.Sheet("cpuinfo-tempdir-short")
        self.addCleanup(sheet.finish)
        for limit in (4, 300):
            for mode in MODES:
                with self.subTest(limit=limit, mode=" ".join(mode)):
                    differential.assert_golden(
                        self, sheet, "limit %d %s" % (limit, " ".join(mode) or "text"), build, mode, limit
                    )


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
