"""check-submit reports a missing temporary directory.

check-submit makes its planner directory in a temporary directory. When no
candidate takes a probe file, the binary reports the eb-stack run as failed
with the text "No usable temporary directory found in [". The case makes every
probe write fail without privileges: the file-size limit caps the size of any
file the binary may write at zero, so the kernel answers the write with EFBIG.
Each run uses a fresh site with an eb-stack stub on PATH. Exit status,
standard output, and standard error are pinned, as text and as JSON, with the
variables that name a temporary directory unset and with one that is long
enough for the message to be cut at 200 characters, in
tests/golden/submit-tempdir.txt.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Dict, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import golden  # noqa: E402
import harness  # noqa: E402

TARGET = "x86_64/amd/zen4"
ARGS = ("--check-submit", "--target", TARGET)
PREFIX = "could not run eb-stack: [Errno 2] No usable temporary directory found in ["
MODES: Sequence[Sequence[str]] = ((), ("--json",))
CASES = (({}, "unset"), ({"TMPDIR": "/nonexistent/" + "d" * 250}, "long"))


def _build(extra: Mapping[str, str]) -> differential.Build:
    def build(site: harness.Site) -> Dict[str, str]:
        prefix = os.path.join(site.root, "prefix")
        modules = os.path.join(prefix, "software", "linux", TARGET, "modules", "all")
        os.makedirs(os.path.join(modules, "R"))
        Path(modules, "R", "4.4.2.lua").write_text("-- module\n", encoding="utf-8")
        eb_stack = Path(site.bindir, "eb-stack")
        eb_stack.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        eb_stack.chmod(0o755)
        env = {
            "PATH": site.bindir + ":/usr/bin:/bin",
            "EESSI_PREFIX": prefix,
            "EESSI_OS_TYPE": "linux",
            "LOADEDMODULES": "EESSI/2025.06:R/4.4.2",
        }
        env.update(extra)
        return env

    return build


class SubmitTempdirTests(unittest.TestCase):
    def test_the_binary_reports_the_missing_directory(self) -> None:
        sheet = golden.Sheet("submit-tempdir")
        self.addCleanup(sheet.finish)
        run = differential.assert_golden(self, sheet, "unset text", _build({}), [*ARGS], fsize=0)
        self.assertEqual(run.returncode, 2, run.stderr)
        self.assertIn(PREFIX, run.stderr)
        run = differential.assert_golden(
            self, sheet, "unset json", _build({}), [*ARGS, "--json"], fsize=0
        )
        self.assertTrue(json.loads(run.stdout)["message"].startswith(PREFIX))
        for extra, label in CASES[1:]:
            for mode in MODES:
                with self.subTest(tmpdir=label, mode=" ".join(mode)):
                    differential.assert_golden(
                        self,
                        sheet,
                        "%s %s" % (label, " ".join(mode) or "text"),
                        _build(extra),
                        [*ARGS, *mode],
                        0,
                    )


def setUpModule() -> None:
    """Run from the tests directory: the temporary directory candidates and PWD name it."""
    unittest.addModuleCleanup(os.chdir, os.getcwd())
    os.chdir(Path(__file__).resolve().parent)


if __name__ == "__main__":
    unittest.main()
