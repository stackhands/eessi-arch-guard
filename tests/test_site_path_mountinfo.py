"""Checks for hostile --mountinfo inputs in share/sitepath.py."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402


class SitePathMountinfoTests(unittest.TestCase):
    def test_mountinfo_rejects_fifo_and_directory(self):
        script = str(ROOT / "share" / "sitepath.py")
        with tempfile.TemporaryDirectory(prefix="eag-sitepath-") as tmp:
            root = Path(tmp)
            fifo = root / "mountinfo.fifo"
            os.mkfifo(fifo, 0o600)
            directory = root / "mountinfo.dir"
            directory.mkdir()
            for path in (fifo, directory):
                with self.subTest(path=path.name):
                    proc = harness.run_with_timeout(
                        [
                            sys.executable,
                            script,
                            "--site",
                            "/opt/site/modules/all",
                            "--eessi",
                            "/cvmfs/software.eessi.io/versions/2026.06/modules/all",
                            "--modulepath",
                            "/opt/site/modules/all:"
                            "/cvmfs/software.eessi.io/versions/2026.06/modules/all",
                            "--mountinfo",
                            str(path),
                        ],
                        timeout=1.5,
                    )
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(proc.stdout, "")
                    self.assertEqual(
                        proc.stderr,
                        f"sitepath.py: cannot read {path}: not a regular file\n",
                    )


if __name__ == "__main__":
    unittest.main()
