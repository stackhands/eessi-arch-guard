"""Tests for docs/tools/check_site.py internal link checks."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_check_site():
    path = ROOT / "docs" / "tools" / "check_site.py"
    spec = importlib.util.spec_from_file_location("check_site", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CheckSiteTests(unittest.TestCase):
    def test_root_relative_links_are_rejected(self):
        cs = load_check_site()
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            (site / "reference").mkdir()
            (site / "reference" / "cli.html").write_text("<h1>cli</h1>", encoding="utf-8")
            (site / "index.html").write_text(
                '<a href="/etc/hosts">host</a>\n'
                '<a href="/reference/cli.html">abs</a>\n',
                encoding="utf-8",
            )
            errors, count = cs.check_site(str(site), [])
        self.assertEqual(count, 2)
        self.assertEqual(len(errors), 2, errors)
        self.assertTrue(any("/etc/hosts" in err for err in errors), errors)
        self.assertTrue(any("/reference/cli.html" in err for err in errors), errors)


if __name__ == "__main__":
    unittest.main()
