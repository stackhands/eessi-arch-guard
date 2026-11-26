"""The Python and Zig guards must report a restore directory that cannot be made.

A restore makes a private directory under the work base twice: the guard
makes one for the helper, and the helper makes one for the Lmod shell code.
When the base cannot be written, Python reports the OSError that mkdtemp
raised, which ends with the path it tried. That path holds eight random
characters, so the cases replace them in both outputs before the comparison.
The guard cases run the whole guard in five output modes, and the helper case
runs the helper alone. The comparison skips when TOOL_BIN is unset, and every
case skips for a user who can write to any directory.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0
DENIED = "[Errno 13] Permission denied: "
GUARD_ARGS = ((), ("--json",), ("--shell",), ("--strict",), ("--strict", "--json"))


class RestoreWorkspaceParityTests(unittest.TestCase):
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

    def _hide_name(self, text: str) -> str:
        pattern = re.escape(self.base) + r"/eessi-arch-guard-[a-z0-9_]{8}"
        return re.sub(pattern, self.base + "/eessi-arch-guard-NAME", text)

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
    def _python() -> List[str]:
        return [sys.executable, "-I", str(harness.GUARD)]

    @staticmethod
    def _zig() -> List[str]:
        return [str(harness.PROGRAM)]

    def test_python_guard_reports_the_mkdtemp_errno(self) -> None:
        proc = self._guard(self._python(), "--json")
        message = json.loads(proc.stdout)["message"]
        self.assertIn("restore workspace unavailable: " + DENIED + "'" + self.base, message)
        proc = self._helper(self._python())
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("restore failed: " + DENIED + "'" + self.base, proc.stderr)

    def test_programs_agree_when_the_restore_directory_cannot_be_made(self) -> None:
        harness.skip_unless_tool_bin(self)
        cases = [(" ".join(args) or "plain", args) for args in GUARD_ARGS]
        for label, args in cases:
            python = self._guard(self._python(), *args)
            program = self._guard(self._zig(), *args)
            with self.subTest(case="guard " + label):
                self.assertEqual(program.returncode, python.returncode)
                self.assertEqual(self._hide_name(program.stderr), self._hide_name(python.stderr))
                self.assertEqual(self._hide_name(program.stdout), self._hide_name(python.stdout))
        python = self._helper(self._python())
        program = self._helper(self._zig())
        with self.subTest(case="helper"):
            self.assertEqual(program.returncode, python.returncode)
            self.assertEqual(self._hide_name(program.stderr), self._hide_name(python.stderr))
            self.assertEqual(self._hide_name(program.stdout), self._hide_name(python.stdout))


if __name__ == "__main__":
    unittest.main()
