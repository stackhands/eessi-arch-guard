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

import os
import shutil
import sys
import unittest
from pathlib import Path
from typing import Dict, Sequence

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
    shutil.copy(FIXTURE / "emeraldrapids-kvm-no-waitpkg.cpuinfo", site.cpuinfo)
    env = harness.base_env(site, CPU, ["R/4.4.2"], gpu="none")
    visible = sorted(c for c in os.sched_getaffinity(0) if c <= 7)[:2]
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


if __name__ == "__main__":
    unittest.main()
