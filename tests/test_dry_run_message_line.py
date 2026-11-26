"""A dry-run reload message stays on one line.

Every dry-run decision message turns a control character into a space and
trims the ends. A node whose EESSI_SOFTWARE_SUBDIR holds a control character
reaches the "would reload" message with that character in the "loaded for"
part, so the message is where a line break would show. The cases run with
and without --json, and ``golden/dry-run-message-line.txt`` pins the exit
status and both streams of each.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import retire_b_support as support  # noqa: E402

CPU = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0
LOADED = (
    "x86_64/amd/zen4\n",
    "x86_64/amd/zen4\r",
    "x86_64/amd/zen4\t",
    "x86_64/amd/zen4\x0b",
    "a\nb",
    "x86_64/amd/zen4 \n ",
    "\n",
)
MODES: Sequence[Sequence[str]] = (("--dry-run",), ("--dry-run", "--json"))


class DryRunMessageLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        self.site.add_cpu(CPU)
        self.site.add_module(CPU, "R/4.4.2")
        self.site.write_cpuinfo("zen4")

    def _run(self, loaded: str, mode: Sequence[str]):
        env = harness.base_env(self.site, CPU, ["R/4.4.2"], gpu="none")
        env["EESSI_SOFTWARE_SUBDIR"] = loaded
        return harness.run_with_timeout([*harness.GUARD_ARGV, *mode], env=env, timeout=RUN_TIMEOUT)

    def test_the_message_is_on_one_line(self) -> None:
        proc = self._run("a\nb", ("--dry-run", "--json"))
        message = json.loads(proc.stdout)["message"]
        self.assertEqual(
            message, "eessi-arch-guard: would reload modules for %s (loaded for a b)" % CPU
        )

    def test_output_of_the_dry_run_message(self) -> None:
        sheet = support.sheet(self, "dry-run-message-line")
        for loaded in LOADED:
            for mode in MODES:
                label = "loaded %r %s" % (loaded, " ".join(mode))
                with self.subTest(case=label):
                    proc = self._run(loaded, mode)
                    sheet.check(
                        self,
                        support.label(label),
                        support.render_proc(proc, support.site_work(self.site)),
                    )


if __name__ == "__main__":
    unittest.main()
