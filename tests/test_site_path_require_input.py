"""Checks for --require-input with blank or root paths in share/sitepath.py."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MOUNT = (
    "36 35 0:36 / / rw,relatime - ext4 /dev/sda1 rw\n"
    "47 36 0:47 / /cvmfs/software.eessi.io rw,nosuid,nodev - fuse.cvmfs software.eessi.io rw\n"
)


class SitePathRequireInputTests(unittest.TestCase):
    def test_require_input_rejects_root_and_whitespace_env_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            mountinfo = Path(tmp) / "mountinfo"
            mountinfo.write_text(MOUNT, encoding="utf-8")
            for bad in ("   ", "/"):
                env = {
                    **os.environ,
                    "EESSI_SITE_SOFTWARE_PATH": bad,
                    "EESSI_SOFTWARE_PATH": "/cvmfs/software.eessi.io/modules/all",
                    "MODULEPATH": "/modules/all:/cvmfs/software.eessi.io/modules/all",
                }
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "share" / "sitepath.py"),
                        "--require-input",
                        "--mountinfo",
                        str(mountinfo),
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 2, (bad, proc.stdout, proc.stderr))


if __name__ == "__main__":
    unittest.main()
