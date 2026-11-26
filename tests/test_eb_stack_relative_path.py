"""check-submit runs a relative eb-stack path from the current directory.

check-submit accepts an eb-stack path that is relative. The cases run from
one generated tree with relative paths, with and without a leading dot, for
an eb-stack that succeeds, one in no executable format, and one naming a
missing interpreter. The first test checks the text of each outcome, and
``golden/eb-stack-relative-path.txt`` pins the exit status and both streams.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import retire_b_support as support  # noqa: E402

TARGET = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0
AVAILABLE = "check-submit: 1 loaded module(s) available for " + TARGET
BAD_FORMAT = "could not run eb-stack: [Errno 8] Exec format error: "
ABSENT = "could not run eb-stack: [Errno 2] No such file or directory: "


class EbStackRelativePathTests(unittest.TestCase):
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
        """Return label, option value, and the expected text."""
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
            "TMPDIR": self.site.tmpdir,
            "EESSI_PREFIX": self.prefix,
            "EESSI_OS_TYPE": "linux",
            "LOADEDMODULES": "EESSI/2025.06:R/4.4.2",
        }

    @staticmethod
    def _argv(option: str) -> List[str]:
        return ["--check-submit", "--target", TARGET, "--eb-stack", option]

    def _program(self, option: str):
        argv = [*harness.GUARD_ARGV, *self._argv(option)]
        return harness.run_with_timeout(argv, env=self._env(), timeout=RUN_TIMEOUT, cwd=self.root)

    def test_a_relative_eb_stack_runs(self) -> None:
        for label, option, expected in self._cases():
            with self.subTest(case=label):
                proc = self._program(option)
                self.assertIn(expected, proc.stdout + proc.stderr)

    def test_output_of_a_relative_eb_stack(self) -> None:
        sheet = support.sheet(self, "eb-stack-relative-path")
        for label, option, _expected in self._cases():
            with self.subTest(case=label):
                proc = self._program(option)
                support.check(self, sheet, label, proc, self.site)


if __name__ == "__main__":
    unittest.main()
