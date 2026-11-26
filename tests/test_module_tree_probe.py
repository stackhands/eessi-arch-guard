"""``--check-submit`` tests the module tree of a target by its existence.

``--check-submit`` first asks whether the target's module directory exists.
It asks with a status check, so a directory it may not list still exists,
and the guard goes on to report an empty inventory; only an absent directory
reports the tree as unavailable. The cases run on one generated tree. The
first two tests check the messages, and ``golden/module-tree-probe.txt`` pins
the exit status and both streams of each mode.
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
import retire_b_support as support  # noqa: E402

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


class ModuleTreeProbeTests(unittest.TestCase):
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
            "TMPDIR": self.site.tmpdir,
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

    def _program(self, *args: str) -> Tuple[int, str, str]:
        full = [*harness.GUARD_ARGV, "--check-submit", "--target", ZEN4]
        full += ["--eb-stack", self.planner, *args]
        proc = harness.run_with_timeout(full, env=self.env, timeout=RUN_TIMEOUT)
        return proc.returncode, proc.stdout, proc.stderr

    def test_an_unlistable_tree_is_empty(self) -> None:
        for mode, reason in MODES.items():
            os.chmod(self.modules, mode)
            with self.subTest(mode=oct(mode)):
                code, _out, err = self._program()
                if reason is None:
                    self.assertEqual((code, err), (0, ""))
                else:
                    self.assertEqual(code, 2)
                    self.assertEqual(err, "eessi-arch-guard: check-submit: %s\n" % reason)
                    doc = json.loads(self._program("--json")[1])
                    self.assertEqual(doc["message"], reason)

    def test_an_absent_tree_is_unavailable(self) -> None:
        self._remove_tree()
        code, _out, err = self._program()
        self.assertEqual(code, 2)
        self.assertIn(UNAVAILABLE, err)

    def test_output_of_the_tree_probe(self) -> None:
        sheet = support.sheet(self, "module-tree-probe")
        work = support.site_work(self.site)
        for mode in MODES:
            os.chmod(self.modules, mode)
            for args in ((), ("--json",)):
                label = "mode %s %s" % (oct(mode), " ".join(args) or "text")
                with self.subTest(case=label):
                    sheet.check(self, label, support.render(*self._program(*args), work))
        self._remove_tree()
        for args in ((), ("--json",)):
            label = "absent %s" % (" ".join(args) or "text")
            with self.subTest(case=label):
                sheet.check(self, label, support.render(*self._program(*args), work))


if __name__ == "__main__":
    unittest.main()
