"""The Python and Zig guards must restore alike when the cpuinfo copy is cut short.

A restore runs the helper in a private workspace and the CPU detection
reads the scoped cpuinfo copy that the guard writes there with one
os.write. When a size limit stops that write short, Python goes on with
the short file and detects the CPU from what is in it; the limit has to be
above the largest script the restore writes (the Lmod fragment, under 700
bytes) and below the size of the copy, so that every step before the copy
passes and only the copy is cut. The cpuinfo of the case has a long line
before the flags, so the cut removes the flags and the node is detected
as the generic target instead of the one the full file gives. The site
hands the guard a private TMPDIR, which strict mode takes as its
workspace, so a strict run reaches the restore and the copy as well.

The size of the files written is not stable to the byte from run to run
(the Lmod fragment holds the workspace path), so the limits stay clear of
those sizes. The helper's own tempfile.gettempdir probe is not covered:
its directory is the restore workspace, where the scripts are written first
and are larger than the probe, so no limit fails the probe and passes the
scripts. Each program runs on a fresh site under the shared size limiter,
so the kernel answers with a short write or EFBIG instead of a signal. The
comparison skips when TOOL_BIN is unset.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Dict, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
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
    padded = re.sub(
        r"(vendor_id[^\n]*\n)", lambda m: m.group(1) + "padding\t: " + "x" * PAD + "\n", text
    )
    Path(site.cpuinfo).write_text(padded, encoding="utf-8")
    env = harness.base_env(site, LOADED, ["R/4.4.2"], gpu="none")
    visible = sorted(c for c in os.sched_getaffinity(0) if c <= 7)[:2]
    env["SLURM_CPU_BIND_LIST"] = ",".join(str(c) for c in visible)
    return env


class RestoreCpuinfoLimitParityTests(unittest.TestCase):
    def _python(self, limit: Optional[int], mode: Sequence[str]) -> differential.Result:
        return differential.run_fresh(differential.python_guard(), build, mode, limit)

    def test_python_guard_detects_the_node_from_the_short_copy(self) -> None:
        for policy in ((), ("--strict",)):
            mode = (*policy, "--json")
            with self.subTest(mode=" ".join(mode)):
                full = json.loads(self._python(None, mode).stdout)
                self.assertEqual(full["decision"], "reloaded")
                self.assertEqual(full["now"], "x86_64/intel/icelake")
            for limit in LIMITS:
                with self.subTest(limit=limit, mode=" ".join(mode)):
                    short = json.loads(self._python(limit, mode).stdout)
                    self.assertEqual(short["decision"], "reloaded")
                    self.assertEqual(short["now"], "x86_64/generic")

    def test_programs_agree_when_the_copy_is_cut_short(self) -> None:
        harness.skip_unless_tool_bin(self)
        for limit in LIMITS:
            for mode in MODES:
                with self.subTest(limit=limit, mode=" ".join(mode)):
                    differential.assert_agree(self, build, mode, limit)


if __name__ == "__main__":
    unittest.main()
