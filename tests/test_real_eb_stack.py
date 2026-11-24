"""``--check-submit`` against an unmodified ``eb-stack``.

The tests in test_check_submit.py answer ``stack arch`` with a stand-in that
prints the line the guard looks for. These start the guard as a program, with
the Zig build when TOOL_BIN names it, and read what ``eb-stack`` prints and
exits with. They run when EB_STACK_BIN names an eb-stack program.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import harness

EB_STACK = os.environ.get("EB_STACK_BIN", "")
ZEN4 = "x86_64/amd/zen4"


def _module(root: Path, name: str, extension: str = ".lua") -> None:
    path = root / (name + extension)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("-- module\n", encoding="utf-8")


@unittest.skipUnless(EB_STACK, "EB_STACK_BIN names no eb-stack program")
class RealEbStackCheckSubmit(unittest.TestCase):
    def run_check(self, loaded: str, installed: tuple[str, ...]):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        prefix = Path(tmp.name) / "prefix"
        modules = prefix / "software" / "linux" / ZEN4 / "modules" / "all"
        modules.mkdir(parents=True)
        for name in installed:
            _module(modules, name)
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": tmp.name,
            "TMPDIR": tmp.name,
            "LANG": "C",
            "LC_ALL": "C",
            "EESSI_PREFIX": str(prefix),
            "EESSI_OS_TYPE": "linux",
            "LOADEDMODULES": loaded,
        }
        return subprocess.run(
            [
                *harness.GUARD_ARGV,
                "--check-submit",
                "--json",
                "--target",
                ZEN4,
                "--eb-stack",
                EB_STACK,
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_modules_the_target_has_are_available(self) -> None:
        proc = self.run_check(
            "EESSI/2026.06:Compiler/GCCcore/15.2.0:R/4.4.2",
            ("Compiler/GCCcore/15.2.0", "R/4.4.2"),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["exit_semantic"], "ok", payload)
        self.assertEqual(payload["missing"], [])
        self.assertEqual(payload["loaded"], 2)

    def test_a_module_the_target_lacks_is_named(self) -> None:
        proc = self.run_check(
            "EESSI/2026.06:R/4.4.2:OnlyOther/1.0:Compiler/GCCcore/15.2.0",
            ("R/4.4.2",),
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["exit_semantic"], "missing", payload)
        self.assertEqual(sorted(payload["missing"]), ["Compiler/GCCcore/15.2.0", "OnlyOther/1.0"])

    def test_a_long_module_name_survives_the_planner_identity(self) -> None:
        name = "Long/" + "1" * 200
        absent = self.run_check("EESSI/2026.06:R/4.4.2:" + name, ("R/4.4.2",))
        self.assertEqual(absent.returncode, 1, absent.stdout + absent.stderr)
        self.assertEqual(json.loads(absent.stdout)["missing"], [name])
        present = self.run_check("EESSI/2026.06:R/4.4.2:" + name, ("R/4.4.2", name))
        self.assertEqual(present.returncode, 0, present.stdout + present.stderr)


if __name__ == "__main__":
    unittest.main()
