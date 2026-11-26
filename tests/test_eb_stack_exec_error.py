"""check-submit names the eb-stack program in an exec error.

check-submit runs eb-stack and reports a failed exec as "could not run
eb-stack" followed by the OS error text. The text ends with the program as
the guard started it: the path from the option, or the path found on PATH,
which stays relative when the PATH entry is relative. The path is written
in single quotes with a quote or a newline in the name escaped. The cases
run on one generated tree, from a directory the relative entries refer to.
The first test checks the error text of each case, and
``golden/eb-stack-exec-error.txt`` pins the exit status and both streams.
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
BAD_FORMAT = "[Errno 8] Exec format error: "
ABSENT = "[Errno 2] No such file or directory: "


class EbStackExecErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        self.root = self.site.root
        self.prefix = os.path.join(self.root, "prefix")
        modules = os.path.join(self.prefix, "software", "linux", TARGET, "modules", "all")
        os.makedirs(os.path.join(modules, "R"))
        Path(modules, "R", "4.4.2.lua").write_text("-- module\n", encoding="utf-8")
        self.tools = os.path.join(self.root, "tools")
        os.mkdir(self.tools)
        self.garbage = os.path.join(self.tools, "eb-garbage")
        Path(self.garbage).write_bytes(b"\x01\x02not a program\n")
        self.shebang = os.path.join(self.tools, "eb-shebang")
        Path(self.shebang).write_bytes(b"#!/nonexistent-eessi-arch-guard-interpreter\n")
        self.quoted = os.path.join(self.tools, "eb-it's")
        self.newline = os.path.join(self.tools, "eb\nstack")
        for path in (self.quoted, self.newline):
            Path(path).write_bytes(b"\x01\x02not a program\n")
        for path in (self.garbage, self.shebang, self.quoted, self.newline):
            os.chmod(path, 0o755)

    def _cases(self) -> List[Tuple[str, str, str, str]]:
        """Return label, option value, PATH, and the expected error text."""
        std = "/usr/bin:/bin"
        return [
            ("absolute path, bad format", self.garbage, std, BAD_FORMAT + repr(self.garbage)),
            ("absolute path, missing interpreter", self.shebang, std, ABSENT + repr(self.shebang)),
            ("absolute entry", "eb-garbage", self.tools, BAD_FORMAT + repr(self.garbage)),
            ("relative entry", "eb-garbage", "tools", BAD_FORMAT + "'tools/eb-garbage'"),
            ("relative entry, no shebang", "eb-shebang", "tools", ABSENT + "'tools/eb-shebang'"),
            ("absolute path, quote", self.quoted, std, BAD_FORMAT + repr(self.quoted)),
            ("absolute entry, quote", "eb-it's", self.tools, BAD_FORMAT + repr(self.quoted)),
            ("relative entry, quote", "eb-it's", "tools", BAD_FORMAT + repr("tools/eb-it's")),
            ("absolute entry, newline", "eb\nstack", self.tools, BAD_FORMAT + repr(self.newline)),
            ("relative entry, newline", "eb\nstack", "tools", BAD_FORMAT + repr("tools/eb\nstack")),
        ]

    def _env(self, path: str) -> Dict[str, str]:
        return {
            "PATH": path,
            "TMPDIR": self.site.tmpdir,
            "EESSI_PREFIX": self.prefix,
            "EESSI_OS_TYPE": "linux",
            "LOADEDMODULES": "EESSI/2025.06:R/4.4.2",
        }

    def _argv(self, option: str) -> List[str]:
        return ["--check-submit", "--target", TARGET, "--eb-stack", option]

    def _program(self, option: str, path: str):
        argv = [*harness.GUARD_ARGV, *self._argv(option)]
        env = self._env(path)
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT, cwd=self.root)

    def test_the_exec_error_names_the_program(self) -> None:
        for label, option, path, expected in self._cases():
            with self.subTest(case=label):
                proc = self._program(option, path)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("could not run eb-stack: " + expected, proc.stderr)

    def test_output_when_eb_stack_cannot_exec(self) -> None:
        sheet = support.sheet(self, "eb-stack-exec-error")
        for label, option, path, _expected in self._cases():
            with self.subTest(case=label):
                proc = self._program(option, path)
                support.check(self, sheet, label, proc, self.site)


if __name__ == "__main__":
    unittest.main()
