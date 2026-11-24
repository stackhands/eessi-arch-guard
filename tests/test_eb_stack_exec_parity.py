"""The Python and Zig guards must name the eb-stack program in an exec error.

check-submit runs eb-stack and reports a failed exec as "could not run
eb-stack" followed by the OSError text. Python's text ends with the program
as it was given to subprocess: the path from the option, or the path
shutil.which returned, which keeps a relative PATH entry relative, written as
repr writes it, so a name with a quote or a newline is escaped. The cases
run both programs on one generated tree, from a directory the relative
entries refer to, and compare exit status, standard output, and standard
error. The comparison skips when TOOL_BIN is unset.
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
BAD_FORMAT = "[Errno 8] Exec format error: "
ABSENT = "[Errno 2] No such file or directory: "


class EbStackExecParityTests(unittest.TestCase):
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
        """Return label, option value, PATH, and the text Python reports."""
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

    def _python(self, option: str, path: str):
        argv = [sys.executable, "-I", str(harness.GUARD), *self._argv(option)]
        env = self._env(path)
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT, cwd=self.root)

    def _program(self, option: str, path: str):
        argv = [str(harness.PROGRAM), *self._argv(option)]
        env = self._env(path)
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT, cwd=self.root)

    def test_python_guard_names_the_program_in_the_exec_error(self) -> None:
        for label, option, path, expected in self._cases():
            with self.subTest(case=label):
                proc = self._python(option, path)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("could not run eb-stack: " + expected, proc.stderr)

    def test_programs_agree_when_eb_stack_cannot_exec(self) -> None:
        harness.skip_unless_tool_bin(self)
        for label, option, path, _expected in self._cases():
            python = self._python(option, path)
            program = self._program(option, path)
            with self.subTest(case=label):
                self.assertEqual(program.returncode, python.returncode)
                self.assertEqual(program.stderr, python.stderr)
                self.assertEqual(program.stdout, python.stdout)


if __name__ == "__main__":
    unittest.main()
