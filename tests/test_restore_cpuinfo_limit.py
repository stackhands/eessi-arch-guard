"""A restore detects the node from the scoped cpuinfo copy when it is cut short.

A restore runs the helper in a private workspace and the CPU detection reads
the scoped cpuinfo copy that the binary writes there with one write. When a
size limit stops that write short, the binary goes on with the short file and
detects the CPU from what is in it. The limit has to be above the largest
script the restore writes (the Lmod fragment, under 700 bytes) and below the
size of the copy, so that every step before the copy passes and only the copy
is cut. The cpuinfo of the case has a long line before the flags, so the cut
removes the flags and the node is detected as the generic target instead of
the one the full file gives. The site hands the binary a private TMPDIR,
which strict mode takes as its workspace, so a strict run reaches the restore
and the copy as well.

The size of the files written is not stable to the byte from run to run (the
Lmod fragment holds the workspace path), so the limits stay clear of those
sizes. Each run uses a fresh site under a file-size limit, so the kernel
answers with a short write or EFBIG instead of a signal. The decision of the
unlimited and the cut runs is asserted, and the whole output of each cut run
is pinned in tests/golden/restore-cpuinfo-limit.txt.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from typing import Dict, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import golden  # noqa: E402
import harness  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cpuinfo"
CPUS = ("x86_64/intel/icelake", "x86_64/generic", "x86_64/amd/zen4")
LOADED = "x86_64/amd/zen4"
PAD = 1400
LIMITS = (1000, 1500)
MODES: Sequence[Sequence[str]] = ((), ("--strict",), ("--json",), ("--strict", "--json"))


def build(site: harness.Site) -> Dict[str, str]:
    for cpu in CPUS:
        site.add_cpu(cpu)
        site.add_module(cpu, "R/4.4.2")
    text = (FIXTURE / "emeraldrapids-kvm-no-waitpkg.cpuinfo").read_text(encoding="utf-8")
    text, visible = harness.on_task_cpus(text, 2)
    padded = re.sub(
        r"(vendor_id[^\n]*\n)", lambda m: m.group(1) + "padding\t: " + "x" * PAD + "\n", text
    )
    Path(site.cpuinfo).write_text(padded, encoding="utf-8")
    env = harness.base_env(site, LOADED, ["R/4.4.2"], gpu="none")
    env["SLURM_CPU_BIND_LIST"] = ",".join(str(c) for c in visible)
    return env


class RestoreCpuinfoLimitTests(unittest.TestCase):
    def test_the_binary_detects_the_node_from_the_short_copy(self) -> None:
        sheet = golden.Sheet("restore-cpuinfo-limit")
        self.addCleanup(sheet.finish)
        for policy in ((), ("--strict",)):
            mode = (*policy, "--json")
            with self.subTest(mode=" ".join(mode)):
                run = differential.assert_golden(
                    self, sheet, "unlimited " + " ".join(mode), build, mode, None
                )
                full = json.loads(run.stdout)
                self.assertEqual(full["decision"], "reloaded")
                self.assertEqual(full["now"], "x86_64/intel/icelake")
            for limit in LIMITS:
                with self.subTest(limit=limit, mode=" ".join(mode)):
                    run = differential.assert_golden(
                        self, sheet, "limit %d %s" % (limit, " ".join(mode)), build, mode, limit
                    )
                    short = json.loads(run.stdout)
                    self.assertEqual(short["decision"], "reloaded")
                    self.assertEqual(short["now"], "x86_64/generic")

    def test_the_output_of_a_cut_copy_in_every_mode(self) -> None:
        sheet = golden.Sheet("restore-cpuinfo-limit-modes")
        self.addCleanup(sheet.finish)
        for limit in LIMITS:
            for mode in MODES:
                with self.subTest(limit=limit, mode=" ".join(mode)):
                    differential.assert_golden(
                        self,
                        sheet,
                        "limit %d %s" % (limit, " ".join(mode) or "text"),
                        build,
                        mode,
                        limit,
                    )


if __name__ == "__main__":
    unittest.main()
