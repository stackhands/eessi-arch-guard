"""The completion files and the man page name every option of --help."""

from __future__ import annotations

import re
import unittest

import harness

SHARE = harness.ROOT / "share"


def options() -> set:
    text = harness.run_with_timeout([*harness.GUARD_ARGV, "--help"]).stdout
    return set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", text))


class CliAssetTests(unittest.TestCase):
    def test_help_names_the_options(self) -> None:
        self.assertGreaterEqual(len(options()), 20)

    def test_the_completion_files_name_every_option(self) -> None:
        for name in ("eessi-arch-guard", "_eessi-arch-guard"):
            text = (SHARE / "completions" / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertEqual(sorted(o for o in options() if o not in text), [])

    def test_the_man_page_names_every_option(self) -> None:
        page = (SHARE / "man" / "eessi-arch-guard.1").read_text(encoding="utf-8")
        page = page.replace("\\-", "-").replace("\\fB", "").replace("\\fR", "")
        self.assertEqual(sorted(o for o in options() if o not in page), [])


if __name__ == "__main__":
    unittest.main()
