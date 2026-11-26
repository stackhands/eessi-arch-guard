"""The Python and Zig guards, each run on a site of its own.

tests/differential.py builds one site per program, so the fake Lmod's event
log of one run never reaches the other. The cases here run the comparison
that a shared site made unreliable: both programs under a limit on the size
of any file they write, which a shared log reaches sooner for whichever
program runs second. The comparisons skip when TOOL_BIN is unset.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import harness  # noqa: E402

CPUS = ("x86_64/amd/zen2", "x86_64/generic", "x86_64/amd/zen4")
LOADED = "x86_64/amd/zen4"
MODES = {"json": ("--json",), "plain": (), "strict": ("--strict", "--json")}
LIMITS = (100, 150, 200, 300)


def build(site: harness.Site) -> Mapping[str, str]:
    for cpu in CPUS:
        site.add_cpu(cpu)
        site.add_module(cpu, "R/4.4.2")
    site.write_cpuinfo("zen2")
    return harness.base_env(site, LOADED, ["R/4.4.2"], gpu="none")


class FreshSiteTests(unittest.TestCase):
    def test_programs_agree_under_a_file_size_limit(self) -> None:
        harness.skip_unless_tool_bin(self)
        for limit in LIMITS:
            for name, args in MODES.items():
                with self.subTest(limit=limit, mode=name):
                    python, zig = differential.assert_agree(self, build, args, limit)
                    self.assertLessEqual(python.log_bytes, limit)
                    self.assertLessEqual(zig.log_bytes, limit)

    def test_one_program_does_not_fill_the_others_log(self) -> None:
        harness.skip_unless_tool_bin(self)
        python, zig = differential.run_pair(build, ("--json",), 200)
        alone = differential.run_fresh(differential.python_guard(), build, ("--json",), 200)
        self.assertEqual(python.log_bytes, alone.log_bytes)
        self.assertEqual(zig.log_bytes, alone.log_bytes)


if __name__ == "__main__":
    unittest.main()
