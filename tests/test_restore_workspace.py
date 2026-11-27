"""A restore directory that cannot be made is reported with its errno text.

A restore makes a private directory under the work base twice: the guard
makes one for the helper, and the helper makes one for the Lmod shell code.
When the base cannot be written, the report ends with the permission error
text and the path that was tried. That path holds eight random characters,
which a golden sheet replaces with RND. The guard cases run the whole guard
in five output modes, and the helper case runs the helper alone;
``golden/restore-workspace.txt`` pins the exit status and both streams of
each. Every case skips for a user who can write to any directory.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import retire_b_support as support  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0
DENIED = "[Errno 13] Permission denied: "
GUARD_ARGS = ((), ("--json",), ("--shell",), ("--strict",), ("--strict", "--json"))


class RestoreWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can write to a read-only directory")
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.site.write_cpuinfo("zen2")
        self.base = os.path.join(self.site.root, "workbase")
        os.mkdir(self.base, 0o500)
        self.addCleanup(os.chmod, self.base, 0o700)

    def _env(self) -> Dict[str, str]:
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        env["TMPDIR"] = self.base
        return env

    def _guard(self, program: List[str], *args: str):
        argv = [*program, *args]
        return harness.run_with_timeout(argv, env=self._env(), timeout=RUN_TIMEOUT)

    def _helper(self, program: List[str]):
        env = self._env()
        request = {
            "v": 1,
            "env": env,
            "eessi": "EESSI/2025.06",
            "modules": ["R/4.4.2"],
            "lmod": env["LMOD_CMD"],
            "pin": "",
        }
        argv = [*program, "--restore"]
        return harness.run_with_timeout(
            argv, env=env, timeout=RUN_TIMEOUT, input=json.dumps(request)
        )

    @staticmethod
    def _program() -> List[str]:
        return list(harness.GUARD_ARGV)

    def test_the_mkdtemp_errno_is_reported(self) -> None:
        proc = self._guard(self._program(), "--json")
        message = json.loads(proc.stdout)["message"]
        self.assertIn("restore workspace unavailable: " + DENIED + "'" + self.base, message)
        proc = self._helper(self._program())
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("restore failed: " + DENIED + "'" + self.base, proc.stderr)

    def test_output_when_the_restore_directory_cannot_be_made(self) -> None:
        sheet = support.sheet(self, "restore-workspace")
        work = support.site_work(self.site)
        for args in GUARD_ARGS:
            label = "guard " + (" ".join(args) or "plain")
            with self.subTest(case=label):
                proc = self._guard(self._program(), *args)
                sheet.check(self, label, support.render_proc(proc, work))
        with self.subTest(case="helper"):
            proc = self._helper(self._program())
            sheet.check(self, "helper", support.render_proc(proc, work))


if __name__ == "__main__":
    unittest.main()
