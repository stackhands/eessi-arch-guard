"""The Python and Zig guards must run a relative eb-stack path alike.

check-submit accepts an eb-stack path that is relative, and Python runs it
from the current directory. The cases run both programs from one generated
tree with relative paths, with and without a leading dot, for an eb-stack
that succeeds, one in no executable format, and one naming a missing
interpreter. They compare exit status, standard output, and standard error.
The comparison skips when TOOL_BIN is unset.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

TARGET = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0
AVAILABLE = "check-submit: 1 loaded module(s) available for " + TARGET
BAD_FORMAT = "could not run eb-stack: [Errno 8] Exec format error: "
ABSENT = "could not run eb-stack: [Errno 2] No such file or directory: "


class EbStackRelativeParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        self.root = self.site.root
        self.prefix = os.path.join(self.root, "prefix")
        modules = os.path.join(self.prefix, "software", "linux", TARGET, "modules", "all")
        os.makedirs(os.path.join(modules, "R"))
        Path(modules, "R", "4.4.2.lua").write_text("-- module\n", encoding="utf-8")
        tools = os.path.join(self.root, "tools")
        os.mkdir(tools)
        for name, content in (
            ("eb-ok", b"#!/bin/sh\nexit 0\n"),
            ("eb-garbage", b"\x01\x02not a program\n"),
            ("eb-shebang", b"#!/nonexistent-eessi-arch-guard-interpreter\n"),
        ):
            path = os.path.join(tools, name)
            Path(path).write_bytes(content)
            os.chmod(path, 0o755)

    @staticmethod
    def _cases() -> List[Tuple[str, str, str]]:
        """Return label, option value, and the text Python reports."""
        return [
            ("dot slash, runs", "./tools/eb-ok", AVAILABLE),
            ("plain relative, runs", "tools/eb-ok", AVAILABLE),
            ("dot slash, bad format", "./tools/eb-garbage", BAD_FORMAT + "'./tools/eb-garbage'"),
            ("plain relative, bad format", "tools/eb-garbage", BAD_FORMAT + "'tools/eb-garbage'"),
            ("plain relative, no shebang", "tools/eb-shebang", ABSENT + "'tools/eb-shebang'"),
        ]

    def _env(self) -> Dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin",
            "EESSI_PREFIX": self.prefix,
            "EESSI_OS_TYPE": "linux",
            "LOADEDMODULES": "EESSI/2025.06:R/4.4.2",
        }

    @staticmethod
    def _argv(option: str) -> List[str]:
        return ["--check-submit", "--target", TARGET, "--eb-stack", option]

    def _python(self, option: str):
        argv = [sys.executable, "-I", str(harness.GUARD), *self._argv(option)]
        return harness.run_with_timeout(argv, env=self._env(), timeout=RUN_TIMEOUT, cwd=self.root)

    def _program(self, option: str):
        argv = [str(harness.PROGRAM), *self._argv(option)]
        return harness.run_with_timeout(argv, env=self._env(), timeout=RUN_TIMEOUT, cwd=self.root)

    def test_python_guard_runs_a_relative_eb_stack(self) -> None:
        for label, option, expected in self._cases():
            with self.subTest(case=label):
                proc = self._python(option)
                self.assertIn(expected, proc.stdout + proc.stderr)

    def test_programs_agree_on_a_relative_eb_stack(self) -> None:
        harness.skip_unless_tool_bin(self)
        for label, option, _expected in self._cases():
            python = self._python(option)
            program = self._program(option)
            with self.subTest(case=label):
                self.assertEqual(program.returncode, python.returncode)
                self.assertEqual(program.stderr, python.stderr)
                self.assertEqual(program.stdout, python.stdout)


if __name__ == "__main__":
    unittest.main()
