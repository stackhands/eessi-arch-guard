"""Installed wheels include share/sitepath.py and share/lmod/SitePackage.lua."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import private_tmp  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


class PipShareFilesTests(unittest.TestCase):
    def test_pip_install_places_share_files_under_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "install"
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--prefix", str(prefix), str(ROOT)],
                check=True,
                capture_output=True,
            )
            share_root = prefix / "local" / "share"
            if not share_root.is_dir():
                share_root = prefix / "share"
            share = share_root / "eessi-arch-guard"
            sitepath = share / "sitepath.py"
            regular_file = share / "_regular_file.py"
            sitepackage = share / "lmod" / "SitePackage.lua"
            self.assertTrue(sitepath.is_file(), f"missing {sitepath}")
            self.assertTrue(regular_file.is_file(), f"missing {regular_file}")
            self.assertTrue(sitepackage.is_file(), f"missing {sitepackage}")


if __name__ == "__main__":
    unittest.main()
