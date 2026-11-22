"""A test run keeps its temporary files out of the shared temporary directory.

The sites of the tests, and the restore workspaces, scoped cpuinfo copies and
job cache files of the guards they start, went to /tmp. A cache file there is
named by the user and the job alone, so two runs of the suite at the same time
could read, replace or remove the file of the other.
"""

from __future__ import annotations

import os
import tempfile
import unittest

import harness

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"


class PrivateTmpTests(unittest.TestCase):
    def test_the_process_has_a_temporary_directory_of_its_own(self) -> None:
        root = tempfile.gettempdir()
        self.assertEqual(os.environ.get("TMPDIR"), root)
        self.assertTrue(os.path.basename(root).startswith("eag-tests-"), root)
        st = os.stat(root)
        self.assertEqual(st.st_uid, os.getuid())
        self.assertEqual(st.st_mode & 0o777, 0o700)

    def test_a_guard_started_from_base_env_caches_in_its_site(self) -> None:
        site = harness.Site()
        self.addCleanup(site.close)
        for cpu in (ZEN2, ZEN4):
            site.add_cpu(cpu)
            site.add_module(cpu, "R/4.4.2")
        site.write_cpuinfo("zen2")
        env = harness.base_env(site, ZEN4, ["R/4.4.2"])
        env["SLURM_JOB_ID"] = "4343"
        env["SLURM_STEP_ID"] = "batch"
        proc = harness.run_guard(env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(site.tmpdir.startswith(site.root + os.sep), site.tmpdir)
        cached = [name for name in os.listdir(site.tmpdir) if name.startswith(".eessi-arch-guard.")]
        self.assertTrue(cached, os.listdir(site.tmpdir))


if __name__ == "__main__":
    unittest.main()
