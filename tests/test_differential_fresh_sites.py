"""The binary, run on a site of its own.

tests/differential.py builds one site per run, so the fake Lmod's event log of
one run never reaches another. The cases here pin the output of the binary
under a limit on the size of any file it writes in
tests/golden/differential-fresh-sites.txt: a shared log would reach the cap
sooner for whichever run comes second, and a fresh site per run keeps the
output the same from run to run.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import golden  # noqa: E402
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
    def test_the_output_is_pinned_under_a_file_size_limit(self) -> None:
        sheet = golden.Sheet("differential-fresh-sites")
        self.addCleanup(sheet.finish)
        for limit in LIMITS:
            for name, args in MODES.items():
                with self.subTest(limit=limit, mode=name):
                    run = differential.assert_golden(
                        self, sheet, "limit %d %s" % (limit, name), build, args, limit
                    )
                    self.assertLessEqual(run.log_bytes, limit)

    def test_one_run_does_not_fill_the_log_of_the_next(self) -> None:
        first = differential.run_fresh(differential.zig_program(), build, ("--json",), 200)
        second = differential.run_fresh(differential.zig_program(), build, ("--json",), 200)
        self.assertEqual(second.log_bytes, first.log_bytes)
        self.assertEqual(second.observed(), first.observed())


if __name__ == "__main__":
    unittest.main()
