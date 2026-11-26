"""The README names every option and environment variable the program reads."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DocsSurfaceTests(unittest.TestCase):
    def test_readme_lists_every_flag_and_env_name(self):
        main = (ROOT / "zig" / "main.zig").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        start = main.index("fn parseArgs")
        body = main[start : main.index("\nfn ", start + 1)]
        flags = set(re.findall(r'"(-{1,2}[A-Za-z][A-Za-z0-9-]*)', body))
        self.assertGreaterEqual(
            flags, {"--shell", "--restore", "--json", "--help", "--version", "--chain", "-h"}
        )
        self.assertEqual(sorted(flag for flag in flags if flag not in readme), [])
        names = set()
        for path in sorted((ROOT / "zig").glob("*.zig")):
            if path.name == "pylower_table.zig" or path.name.endswith("_test.zig"):
                continue
            source = path.read_text(encoding="utf-8").split("\ntest ")[0]
            names |= set(re.findall(r'"([A-Z][A-Z0-9_]{1,})"', source))
        names = {name for name in names if not name.startswith("SIG")}
        self.assertEqual(sorted(name for name in names if name not in readme), [])

    def test_license_expression_has_no_license_classifier(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('license = "MIT"', text)
        self.assertNotIn("License :: OSI Approved", text)


if __name__ == "__main__":
    unittest.main()
