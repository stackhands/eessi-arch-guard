"""The guard goes on along PATH when an exec of bash fails.

The guard runs bash from PATH and tries each entry in turn: an entry whose
exec fails, because the file is in no executable format or its interpreter is
missing, does not end the search, and a later entry holding a working bash
runs. When no entry runs, the first error that is not "no such file" or "not
a directory" is reported, and when every entry fails with one of those two
the error of the last entry is reported. The cases run on one generated tree.
The first test checks the error of each case, and
``golden/exec-path-fallthrough.txt`` pins the exit status and both streams.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Dict, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import retire_b_support as support  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0
MISSING = "/nonexistent-eessi-arch-guard-dir"
WORKING = "/usr/bin:/bin"
BAD_FORMAT = "[Errno 8] Exec format error: 'bash'"
DENIED = "[Errno 13] Permission denied: 'bash'"
ABSENT = "[Errno 2] No such file or directory: 'bash'"
NOT_A_DIRECTORY = "[Errno 20] Not a directory: 'bash'"


class ExecPathFallthroughTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.site.write_cpuinfo("zen2")
        self.dirs: Dict[str, str] = {}
        for kind, content, mode in (
            ("garbage", b"\x01\x02not a program\n", 0o755),
            ("shebang", b"#!/nonexistent-eessi-arch-guard-interpreter\n", 0o755),
            ("plain", b"#!/bin/sh\n", 0o644),
        ):
            directory = os.path.join(self.site.root, kind)
            os.mkdir(directory)
            path = os.path.join(directory, "bash")
            Path(path).write_bytes(content)
            os.chmod(path, mode)
            self.dirs[kind] = directory
        garbage, shebang, plain = (self.dirs[k] for k in ("garbage", "shebang", "plain"))
        regular_file = os.path.join(self.site.root, "regular-file")
        Path(regular_file).write_bytes(b"not a directory\n")
        self.cases: Dict[str, Tuple[str, str]] = {
            "bad format, then a working bash": (garbage + ":" + WORKING, ""),
            "missing interpreter, then a working bash": (shebang + ":" + WORKING, ""),
            "bad format, then absent": (garbage + ":" + MISSING, BAD_FORMAT),
            "bad format, then no execute permission": (garbage + ":" + plain, BAD_FORMAT),
            "no execute permission, then bad format": (plain + ":" + garbage, DENIED),
            "absent, then not a directory": (MISSING + ":" + regular_file, NOT_A_DIRECTORY),
            "not a directory, then absent": (regular_file + ":" + MISSING, ABSENT),
            "absent, then bad format, then a working bash": (
                MISSING + ":" + garbage + ":" + WORKING,
                "",
            ),
        }

    def _env(self, path: str) -> Dict[str, str]:
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        env["PATH"] = path
        return env

    def _program(self, env: Dict[str, str]):
        argv = [*harness.GUARD_ARGV, "--dry-run", "--json"]
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT)

    def test_the_search_goes_on_past_a_failed_exec(self) -> None:
        for label, (path, expected) in self.cases.items():
            with self.subTest(case=label):
                proc = self._program(self._env(path))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                if expected:
                    self.assertIn("could not run archdetect: " + expected, proc.stdout)
                else:
                    self.assertNotIn("could not run archdetect", proc.stdout)

    def test_output_when_an_exec_fails_along_path(self) -> None:
        sheet = support.sheet(self, "exec-path-fallthrough")
        for label, (path, _expected) in self.cases.items():
            with self.subTest(case=label):
                proc = self._program(self._env(path))
                sheet.check(
                    self, support.label(label), support.render_proc(proc, support.site_work(self.site))
                )


if __name__ == "__main__":
    unittest.main()
