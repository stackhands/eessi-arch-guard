"""The Python and Zig guards must report a failed planner write alike.

check-submit writes its planner inputs to a private directory and, when a
write fails, reports "could not run eb-stack" followed by the OSError text.
The cases make a real write fail without privileges: differential.run_fresh
starts the guard under the shared size limiter, so the kernel answers a
write past the cap with EFBIG. The cap is above the few bytes Python's
temporary-directory probe writes and below the size of the easystack file,
so the failure falls on the first planner file in both programs. Each
program runs on a site of its own, and they are compared on exit status,
standard output, and standard error. The comparison skips when TOOL_BIN is
unset.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
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


class SubmitWriteErrnoParityTests(unittest.TestCase):
    def test_python_guard_reports_the_write_errno(self) -> None:
        python = differential.run_fresh(differential.python_guard(), build, ARGS, LIMIT)
        self.assertEqual(python.returncode, 2, python.stderr)
        self.assertIn(EXPECTED, python.stderr)

    def test_programs_agree_when_a_planner_write_fails(self) -> None:
        harness.skip_unless_tool_bin(self)
        differential.assert_agree(self, build, ARGS, LIMIT)


if __name__ == "__main__":
    unittest.main()
