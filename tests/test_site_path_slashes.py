"""Slash normalization checks for share/sitepath.py."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_sitepath():
    path = ROOT / "share" / "sitepath.py"
    spec = importlib.util.spec_from_file_location("sitepath", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


MOUNT = (
    "36 35 0:36 / / rw,relatime - ext4 /dev/sda1 rw\n"
    "47 36 0:47 / /cvmfs/software.eessi.io rw,nosuid,nodev - fuse.cvmfs software.eessi.io rw\n"
)


class SitePathSlashTests(unittest.TestCase):
    def test_repeated_slashes_normalize_in_mount_and_modulepath_checks(self):
        sp = load_sitepath()
        self.assertEqual(
            sp.client_mount(MOUNT, "/cvmfs//software.eessi.io"),
            "fuse.cvmfs",
        )
        site = "/opt/site/modules/all"
        eessi = (
            "/cvmfs/software.eessi.io/versions/2026.06"
            "/software/linux/x86_64/amd/zen4/modules/all"
        )
        self.assertEqual(
            sp.order("/opt/site//modules/all:" + eessi, site, eessi),
            "site_first",
        )


if __name__ == "__main__":
    unittest.main()
