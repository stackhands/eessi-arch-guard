"""The EESSI client mount and the site module path in front of it."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    path = ROOT / "share" / "sitepath.py"
    spec = importlib.util.spec_from_file_location("sitepath", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOUNT = """\
36 35 0:36 / / rw,relatime - ext4 /dev/sda1 rw
47 36 0:47 / /cvmfs/software.eessi.io rw,nosuid,nodev - fuse.cvmfs software.eessi.io rw
"""


class SitePathTests(unittest.TestCase):
    def test_a_cvmfs_mount_holds_the_repo_and_the_root_disk_does_not(self):
        sp = load()
        self.assertEqual(sp.client_mount(MOUNT, "/cvmfs/software.eessi.io"), "fuse.cvmfs")
        self.assertIsNone(sp.client_mount(MOUNT, "/cvmfs/other.example"))
        plain = "36 35 0:36 / / rw - ext4 /dev/sda1 rw\n"
        self.assertIsNone(sp.client_mount(plain, "/cvmfs/software.eessi.io"))

    def test_the_site_path_has_to_precede_the_eessi_path(self):
        sp = load()
        site = "/opt/site/modules/all"
        eessi = (
            "/cvmfs/software.eessi.io/versions/2026.06"
            "/software/linux/x86_64/amd/zen4/modules/all"
        )
        self.assertEqual(sp.order(f"{site}:{eessi}", site, eessi), "site_first")
        self.assertEqual(sp.order(f"{eessi}:{site}", site, eessi), "eessi_first")
        self.assertEqual(sp.order(f"{site}/:{eessi}/", site, eessi), "site_first")
        self.assertEqual(sp.order(eessi, site, eessi), "site_missing")
        self.assertEqual(sp.order(site, site, eessi), "eessi_missing")
        # /opt/site-extra must not count as /opt/site
        self.assertEqual(
            sp.order("/opt/site-extra/modules/all:" + eessi, site, eessi),
            "site_missing",
        )

    def test_both_checks_pass_only_together(self):
        sp = load()
        site = "/opt/site/modules/all"
        eessi = (
            "/cvmfs/software.eessi.io/versions/2026.06"
            "/software/linux/x86_64/amd/zen4/modules/all"
        )
        code, text = sp.report(MOUNT, f"{site}:{eessi}", "/cvmfs/software.eessi.io", site, eessi)
        self.assertEqual(code, 0, text)
        self.assertIn("ok", text)
        self.assertNotIn("FAIL", text)
        code, text = sp.report(MOUNT, f"{eessi}:{site}", "/cvmfs/software.eessi.io", site, eessi)
        self.assertEqual(code, 1)
        self.assertIn("behind", text)
        plain = "36 35 0:36 / / rw - ext4 /dev/sda1 rw\n"
        code, text = sp.report(plain, f"{site}:{eessi}", "/cvmfs/software.eessi.io", site, eessi)
        self.assertEqual(code, 1)
        self.assertIn("not on a cvmfs mount", text)

    def test_the_how_to_names_the_command_and_the_default(self):
        page = (ROOT / "docs" / "orgmode" / "howto" / "check-the-client-mount.org").read_text()
        self.assertIn("share/sitepath.py", page)
        self.assertIn("site modules", page)


if __name__ == "__main__":
    unittest.main()
