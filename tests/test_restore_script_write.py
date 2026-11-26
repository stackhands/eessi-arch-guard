"""A failed write of the restore scripts is reported.

The restore helper writes the shell code Lmod printed to a private file
before it runs it. When that write fails, the helper reports "restore failed"
followed by the error text and exits with status 1. The case runs the helper
alone on a fresh site, under a file-size limit of zero, so the kernel answers
the first write with EFBIG. The Lmod command is a script that only prints,
because the test Lmod keeps a log file. The exit status and both streams are
asserted and pinned in tests/golden/restore-script-write.txt.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Dict, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import golden  # noqa: E402
import harness  # noqa: E402

ZEN4 = "x86_64/amd/zen4"
EXPECTED = "eessi-arch-guard: restore failed: [Errno 27] File too large\n"


def build(site: harness.Site) -> Dict[str, str]:
    site.add_cpu(ZEN4)
    site.add_module(ZEN4, "R/4.4.2")
    base = os.path.join(site.root, "workbase")
    os.mkdir(base, 0o700)
    lmod = Path(site.root, "quiet-lmod")
    lmod.write_text("#!/bin/sh\necho 'export QUIET=1'\n", encoding="utf-8")
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
            "modules": ["R/4.4.2"],
            "lmod": env["LMOD_CMD"],
            "pin": "",
        }
    )


class RestoreScriptWriteTests(unittest.TestCase):
    def test_the_helper_reports_the_write_errno(self) -> None:
        sheet = golden.Sheet("restore-script-write")
        self.addCleanup(sheet.finish)
        run = differential.assert_golden(
            self, sheet, "efbig", build, ["--restore"], fsize=0, input_for=request
        )
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertEqual(run.stdout, "")
        self.assertEqual(run.stderr, EXPECTED)


if __name__ == "__main__":
    unittest.main()
