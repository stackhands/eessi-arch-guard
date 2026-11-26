"""check-submit reports a failed planner write.

check-submit writes its planner inputs to a private directory and, when a
write fails, reports "could not run eb-stack" followed by the error text. The
case makes a real write fail without privileges: differential.run_fresh starts
the binary under a file-size limit, so the kernel answers a write past the cap
with EFBIG. The cap is above the few bytes a temporary-directory probe writes
and below the size of the easystack file, so the failure falls on the first
planner file. Exit status and both streams are asserted and pinned in
tests/golden/submit-write-errno.txt.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import golden  # noqa: E402
import harness  # noqa: E402

TARGET = "x86_64/amd/zen4"
LIMIT = 20
ARGS = ("--check-submit", "--target", TARGET)
EXPECTED = "could not run eb-stack: [Errno 27] File too large"


def build(site: harness.Site) -> Dict[str, str]:
    """A module tree for TARGET and an eb-stack on PATH that would succeed."""
    prefix = os.path.join(site.root, "prefix")
    modules = os.path.join(prefix, "software", "linux", TARGET, "modules", "all")
    os.makedirs(os.path.join(modules, "R"))
    Path(modules, "R", "4.4.2.lua").write_text("-- module\n", encoding="utf-8")
    eb_stack = Path(site.bindir, "eb-stack")
    eb_stack.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    eb_stack.chmod(0o755)
    return {
        "PATH": site.bindir + ":/usr/bin:/bin",
        "TMPDIR": site.tmpdir,
        "EESSI_PREFIX": prefix,
        "EESSI_OS_TYPE": "linux",
        "LOADEDMODULES": "EESSI/2025.06:R/4.4.2",
    }


class SubmitWriteErrnoTests(unittest.TestCase):
    def test_the_binary_reports_the_write_errno(self) -> None:
        sheet = golden.Sheet("submit-write-errno")
        self.addCleanup(sheet.finish)
        run = differential.assert_golden(self, sheet, "efbig", build, ARGS, LIMIT)
        self.assertEqual(run.returncode, 2, run.stderr)
        self.assertIn(EXPECTED, run.stderr)


if __name__ == "__main__":
    unittest.main()
