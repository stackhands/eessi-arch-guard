"""The Python and Zig guards must report a failed exec by its errno alike.

Python raises the OSError that exec produced, so a program that exists but is
not a valid executable ("Exec format error") or whose interpreter is missing
("No such file or directory") is reported with its own text and the program
name. The guard reaches such a program in two places: bash on PATH, which
reads the archdetect answer, and the Lmod command, which restores modules. The
cases run both programs on one generated tree and compare exit status,
standard output, and standard error. The comparison skips when TOOL_BIN is
unset.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0
GARBAGE = b"\x01\x02not a program\n"
SHEBANG = b"#!/nonexistent-eessi-arch-guard-interpreter\n"
BAD_FORMAT = "[Errno 8] Exec format error: "
ABSENT = "[Errno 2] No such file or directory: "
QUOTED_NAMES = {
    "single quote": "it's lmod",
    "both quotes": "a'b\"c",
}


class ExecErrnoParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.site.write_cpuinfo("zen2")
        self.programs: Dict[str, str] = {}
        for kind, content in (("garbage", GARBAGE), ("shebang", SHEBANG)):
            directory = os.path.join(self.site.root, kind)
            os.mkdir(directory)
            path = os.path.join(directory, "bash")
            Path(path).write_bytes(content)
            os.chmod(path, 0o755)
            self.programs[kind] = path
        for kind, name in QUOTED_NAMES.items():
            path = os.path.join(self.site.root, name)
            Path(path).write_bytes(GARBAGE)
            os.chmod(path, 0o755)
            self.programs[kind] = path

    def _env(self) -> Dict[str, str]:
        return harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")

    def _cases(self) -> List[Tuple[str, Dict[str, str], List[str], str]]:
        cases = []
        for kind, expected in (("garbage", BAD_FORMAT), ("shebang", ABSENT)):
            env = self._env()
            env["PATH"] = os.path.dirname(self.programs[kind])
            cases.append(
                (
                    "bash on PATH, " + kind,
                    env,
                    ["--dry-run", "--json"],
                    "could not run archdetect: " + expected + "'bash'",
                )
            )
            env = self._env()
            env["LMOD_CMD"] = self.programs[kind]
            cases.append(
                (
                    "Lmod command, " + kind,
                    env,
                    ["--json"],
                    "restore failed: " + expected + "'" + self.programs[kind] + "'",
                )
            )
        for kind in QUOTED_NAMES:
            env = self._env()
            env["LMOD_CMD"] = self.programs[kind]
            cases.append(
                (
                    "Lmod command, " + kind,
                    env,
                    ["--json"],
                    "restore failed: " + BAD_FORMAT + repr(self.programs[kind]),
                )
            )
        return cases

    @staticmethod
    def _python(env: Dict[str, str], args: List[str]):
        argv = [sys.executable, "-I", str(harness.GUARD), *args]
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT)

    @staticmethod
    def _program(env: Dict[str, str], args: List[str]):
        argv = [str(harness.PROGRAM), *args]
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT)

    def test_python_guard_reports_the_exec_errno(self) -> None:
        for label, env, args, expected in self._cases():
            with self.subTest(case=label):
                proc = self._python(env, args)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(expected, json.loads(proc.stdout)["message"])

    def test_programs_agree_when_exec_fails(self) -> None:
        harness.skip_unless_tool_bin(self)
        for label, env, args, _expected in self._cases():
            python = self._python(env, args)
            program = self._program(env, args)
            with self.subTest(case=label):
                self.assertEqual(program.returncode, python.returncode)
                self.assertEqual(program.stderr, python.stderr)
                self.assertEqual(program.stdout, python.stdout)


if __name__ == "__main__":
    unittest.main()
