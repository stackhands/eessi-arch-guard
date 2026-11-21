"""The Python and Zig guards, each run on a site of its own.

tests/differential.py builds one site per program, so the fake Lmod's event
log of one run never reaches the other. The cases here show that each run
gets a site of its own that is removed afterwards, that the random workspace
names are normalized, and that the size limiter caps what a program writes.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import harness  # noqa: E402

CPUS = ("x86_64/amd/zen2", "x86_64/generic", "x86_64/amd/zen4")
LOADED = "x86_64/amd/zen4"


def build(site: harness.Site) -> Mapping[str, str]:
    for cpu in CPUS:
        site.add_cpu(cpu)
        site.add_module(cpu, "R/4.4.2")
    site.write_cpuinfo("zen2")
    return harness.base_env(site, LOADED, ["R/4.4.2"], gpu="none")


class FreshSiteTests(unittest.TestCase):
    def test_each_run_has_a_site_of_its_own(self) -> None:
        first = differential.run_fresh(differential.python_guard(), build, ("--json",))
        second = differential.run_fresh(differential.python_guard(), build, ("--json",))
        self.assertNotEqual(first.site_root, second.site_root)
        self.assertFalse(os.path.exists(first.site_root))
        self.assertGreater(first.log_bytes, 0)
        self.assertEqual(second.log_bytes, first.log_bytes)
        self.assertEqual(second.observed(), first.observed())

    def test_random_workspace_names_are_normalized(self) -> None:
        text = "/tmp/eessi-arch-guard-ab12_x9z/lmod.log /tmp/eessi-arch-guard-cpu-q1w2e3r4"
        self.assertEqual(differential.normalize(text), "/tmp/RND/lmod.log /tmp/RND")

    def test_the_size_limiter_caps_the_files_a_program_writes(self) -> None:
        free = differential.run_fresh(differential.python_guard(), build, ("--json",))
        limit = free.log_bytes // 2
        capped = differential.run_fresh(differential.python_guard(), build, ("--json",), limit)
        self.assertGreater(limit, 0)
        self.assertEqual(capped.log_bytes, limit)


if __name__ == "__main__":
    unittest.main()
