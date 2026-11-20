"""The Python and Zig guards must print a dry-run reload message on one line.

Python builds every dry-run decision message with one_line, which turns a
control character into a space and trims the ends. A node whose
EESSI_SOFTWARE_SUBDIR holds a control character reaches the "would reload"
message with that character in the "loaded for" part, so the message is
where a difference shows. The programs are compared on exit status, standard
output, and standard error, with and without --json. The comparison skips
when TOOL_BIN is unset.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

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


class DryRunMessageLineParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        self.site.add_cpu(CPU)
        self.site.add_module(CPU, "R/4.4.2")
        self.site.write_cpuinfo("zen4")

    def _run(self, guard, loaded: str, mode: Sequence[str]):
        env = harness.base_env(self.site, CPU, ["R/4.4.2"], gpu="none")
        env["EESSI_SOFTWARE_SUBDIR"] = loaded
        return harness.run_with_timeout([*guard, *mode], env=env, timeout=RUN_TIMEOUT)

    def test_python_guard_prints_the_message_on_one_line(self) -> None:
        guard = [sys.executable, "-I", str(harness.GUARD)]
        proc = self._run(guard, "a\nb", ("--dry-run", "--json"))
        message = json.loads(proc.stdout)["message"]
        self.assertEqual(
            message, "eessi-arch-guard: would reload modules for %s (loaded for a b)" % CPU
        )

    def test_programs_agree_on_the_dry_run_message(self) -> None:
        harness.skip_unless_tool_bin(self)
        for loaded in LOADED:
            for mode in MODES:
                with self.subTest(loaded=loaded, mode=" ".join(mode)):
                    python = self._run([sys.executable, "-I", str(harness.GUARD)], loaded, mode)
                    program = self._run([str(harness.PROGRAM)], loaded, mode)
                    self.assertEqual(program.returncode, python.returncode, program.stderr)
                    self.assertEqual(program.stderr, python.stderr)
                    self.assertEqual(program.stdout, python.stdout)


if __name__ == "__main__":
    unittest.main()
