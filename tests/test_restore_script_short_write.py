"""The restore helper goes on with a script that is written short.

The restore helper writes the shell code Lmod printed to a private file with
one write and runs it. When a size limit stops that write short, the helper
does not fail: the file holds the first bytes of the code and bash runs what
is there.

The helper is run alone, as the restore parent runs it, with the request on
standard input, on a fresh site. The file-size limit caps the size of any file
the helper may write, so the kernel answers with a short write. The Lmod
command prints a fixed script, so the point of the cut does not depend on a
path or on timing: the limit lies inside a comment line, above the size of the
wrapper script, and the line that sets the marker variable comes after it. The
whole output of the unlimited and the cut run is pinned in
tests/golden/restore-script-short-write.txt.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Dict, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import golden  # noqa: E402
import harness  # noqa: E402

ZEN4 = "x86_64/amd/zen4"
LIMIT = 100
FRAGMENT = "export LEAD=1\\n# " + "x" * 120 + "\\nexport MARKER=full\\n"


def build(site: harness.Site) -> Dict[str, str]:
    site.add_cpu(ZEN4)
    site.add_module(ZEN4, "R/4.4.2")
    base = os.path.join(site.root, "workbase")
    os.mkdir(base, 0o700)
    lmod = Path(site.root, "fixed-lmod")
    lmod.write_text("#!/bin/sh\nprintf '" + FRAGMENT + "'\n", encoding="utf-8")
    lmod.chmod(0o755)
    env = harness.base_env(site, ZEN4, ["R/4.4.2"], gpu="none")
    env["TMPDIR"] = base
    env["LMOD_CMD"] = str(lmod)
    return env


def request(env: Mapping[str, str]) -> str:
    return json.dumps(
        {
            "v": 1,
            "env": dict(env),
            "eessi": "EESSI/2025.06",
            "modules": [],
            "lmod": env["LMOD_CMD"],
            "pin": "",
        }
    )


def exported(stdout: str) -> Optional[Dict[str, str]]:
    return json.loads(stdout).get("env")


class RestoreScriptShortWriteTests(unittest.TestCase):
    def _run(self, sheet: golden.Sheet, label: str, limit: Optional[int]) -> differential.Result:
        return differential.assert_golden(self, sheet, label, build, ["--restore"], limit, request)

    def test_the_helper_goes_on_with_the_short_script(self) -> None:
        sheet = golden.Sheet("restore-script-short-write")
        self.addCleanup(sheet.finish)
        full = self._run(sheet, "unlimited", None)
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertEqual(exported(full.stdout)["MARKER"], "full")
        short = self._run(sheet, "limit %d" % LIMIT, LIMIT)
        self.assertEqual(short.returncode, 0, short.stderr)
        env = exported(short.stdout)
        self.assertEqual(env["LEAD"], "1")
        self.assertNotIn("MARKER", env)


def setUpModule() -> None:
    """Run from the tests directory: the temporary directory candidates and PWD name it."""
    unittest.addModuleCleanup(os.chdir, os.getcwd())
    os.chdir(Path(__file__).resolve().parent)


if __name__ == "__main__":
    unittest.main()
