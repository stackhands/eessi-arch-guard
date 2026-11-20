"""The Python and Zig guards must test the module tree of a target alike.

``--check-submit`` first asks whether the target's module directory exists.
The Python guard asks with a status check, so a directory it may not list
still exists, and the guard goes on to report an empty inventory. A program
that opens the directory instead reports the tree as unavailable. The cases
run both programs on one generated tree and compare exit status, standard
output, and standard error. The comparison skips when TOOL_BIN is unset.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from typing import Dict, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

ZEN4 = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0
EMPTY = "submit target has empty module inventory: " + ZEN4
UNAVAILABLE = "target module tree is unavailable: "

MODES = {
    0o755: None,
    0o500: None,
    0o300: EMPTY,
    0o100: EMPTY,
    0o200: EMPTY,
    0o000: EMPTY,
}


class ModuleTreeProbeParityTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can list a directory whatever its mode")
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        self.prefix = Path(self.site.root) / "submit-prefix"
        self.modules = self.prefix / "software" / "linux" / ZEN4 / "modules" / "all"
        (self.modules / "R").mkdir(parents=True)
        (self.modules / "R" / "4.4.2.lua").write_text("-- module\n", encoding="utf-8")
        self.addCleanup(self._unlock)
        planner = Path(self.site.root) / "eb-stack"
        planner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        planner.chmod(0o755)
        self.planner = str(planner)
        self.env: Dict[str, str] = {
            "PATH": "/usr/bin:/bin",
            "EESSI_PREFIX": str(self.prefix),
            "EESSI_OS_TYPE": "linux",
            "LOADEDMODULES": "EESSI/2025.06:R/4.4.2",
        }

    def _unlock(self) -> None:
        if self.modules.is_dir():
            os.chmod(self.modules, 0o700)

    def _remove_tree(self) -> None:
        os.chmod(self.modules, 0o700)
        shutil.rmtree(self.modules)

    def _run(self, argv, *args: str) -> Tuple[int, str, str]:
        full = [*argv, "--check-submit", "--target", ZEN4, "--eb-stack", self.planner, *args]
        proc = harness.run_with_timeout(full, env=self.env, timeout=RUN_TIMEOUT)
        return proc.returncode, proc.stdout, proc.stderr

    def _python(self, *args: str) -> Tuple[int, str, str]:
        return self._run([sys.executable, "-I", str(harness.GUARD)], *args)

    def _program(self, *args: str) -> Tuple[int, str, str]:
        return self._run([str(harness.PROGRAM)], *args)

    def test_python_guard_treats_an_unlistable_tree_as_empty(self) -> None:
        for mode, reason in MODES.items():
            os.chmod(self.modules, mode)
            with self.subTest(mode=oct(mode)):
                code, _out, err = self._python()
                if reason is None:
                    self.assertEqual((code, err), (0, ""))
                else:
                    self.assertEqual(code, 2)
                    self.assertEqual(err, "eessi-arch-guard: check-submit: %s\n" % reason)
                    doc = json.loads(self._python("--json")[1])
                    self.assertEqual(doc["message"], reason)

    def test_python_guard_reports_an_absent_tree(self) -> None:
        self._remove_tree()
        code, _out, err = self._python()
        self.assertEqual(code, 2)
        self.assertIn(UNAVAILABLE, err)

    def test_programs_agree_on_the_tree_probe(self) -> None:
        harness.skip_unless_tool_bin(self)
        for mode in MODES:
            os.chmod(self.modules, mode)
            for args in ((), ("--json",)):
                with self.subTest(mode=oct(mode), args=args):
                    self.assertEqual(self._program(*args), self._python(*args))
        self._remove_tree()
        for args in ((), ("--json",)):
            with self.subTest(mode="absent", args=args):
                self.assertEqual(self._program(*args), self._python(*args))


if __name__ == "__main__":
    unittest.main()
