"""The Python and Zig guards must report the same failure when no temporary
directory takes a file.

check-submit makes its planner directory with tempfile.mkdtemp, which takes
the directory from tempfile.gettempdir. When no candidate takes a probe
file, gettempdir raises FileNotFoundError, and the guard reports the eb-stack
run as failed with that text. The case makes every probe write fail without
privileges: the shared size limiter caps the size of any file the program
may write at zero, so the kernel answers the write with EFBIG. Each program
runs on a fresh site with an eb-stack stub on PATH. The programs are compared
on exit status, standard output, and standard error, as text and as JSON,
with the variables that name a temporary directory unset and with one that
is long enough for the message to be cut at 200 characters. The comparison
skips when TOOL_BIN is unset.
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


class SubmitTempdirParityTests(unittest.TestCase):
    def _python(self, mode: Sequence[str]) -> differential.Result:
        return differential.run_fresh(
            differential.python_guard(), _build({}), [*ARGS, *mode], fsize=0
        )

    def test_python_guard_reports_the_gettempdir_error(self) -> None:
        run = self._python(())
        self.assertEqual(run.returncode, 2, run.stderr)
        self.assertIn(PREFIX, run.stderr)
        run = self._python(("--json",))
        self.assertTrue(json.loads(run.stdout)["message"].startswith(PREFIX))

    def test_programs_agree_when_no_temporary_directory_takes_a_file(self) -> None:
        harness.skip_unless_tool_bin(self)
        for extra, label in CASES:
            for mode in MODES:
                with self.subTest(tmpdir=label, mode=" ".join(mode)):
                    differential.assert_agree(self, _build(extra), [*ARGS, *mode], 0)


if __name__ == "__main__":
    unittest.main()
