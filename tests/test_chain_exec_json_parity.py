"""The Python and Zig guards must print one JSON object for a chain exec failure.

A chained prolog that cannot be started raises an OSError in Python before
any decision is made. Python's main catches it and, with --json on the
command line, prints a failed decision whose cause is the exception class
and whose message is the exception text; without --json it prints the skip
line. The programs are compared on exit status, standard output, and
standard error for a chain whose interpreter is missing, a chain that is
not a program the kernel can run, and a chain under a directory whose name
has a quote and a character outside ASCII, so that the message has to be
quoted as Python does and escaped as JSON does. The comparison skips when
TOOL_BIN is unset.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

CPU = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0
MODES: Sequence[Sequence[str]] = (
    ("--json",),
    ("--json", "--strict"),
)


class ChainExecJsonParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        self.site.add_cpu(CPU)
        self.site.add_module(CPU, "R/4.4.2")
        self.site.write_cpuinfo("zen4")
        self.bad_shebang = self._script("chain-bad-shebang.sh", b"#!/no/such/interpreter\nexit 0\n")
        self.empty = self._script("chain-empty.sh", b"")
        odd_dir = os.path.join(self.site.root, "it's \u00e9")
        os.makedirs(odd_dir)
        self.odd = self._script("chain.sh", b"#!/no/such/interpreter\nexit 0\n", odd_dir)

    def _script(self, name: str, body: bytes, directory: str = "") -> str:
        path = os.path.join(directory or self.site.root, name)
        Path(path).write_bytes(body)
        os.chmod(path, 0o755)
        return path

    def _run(self, guard, chain: str, mode: Sequence[str]):
        env = harness.base_env(self.site, CPU, ["R/4.4.2"], gpu="none")
        argv = [*guard, "--chain", chain, *mode]
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT)

    def _python(self, chain: str, mode: Sequence[str]):
        return self._run([sys.executable, "-I", str(harness.GUARD)], chain, mode)

    def test_python_guard_prints_the_exception_as_the_message(self) -> None:
        proc = self._python(self.bad_shebang, ("--json",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["cause"], "FileNotFoundError")
        self.assertEqual(
            report["message"], "[Errno 2] No such file or directory: %r" % self.bad_shebang
        )
        self.assertIsNone(report["detail"])
        proc = self._python(self.empty, ("--json",))
        report = json.loads(proc.stdout)
        self.assertEqual(report["cause"], "OSError")
        self.assertEqual(report["message"], "[Errno 8] Exec format error: %r" % self.empty)

    def test_programs_agree_when_the_chain_cannot_be_started(self) -> None:
        harness.skip_unless_tool_bin(self)
        for chain in (self.bad_shebang, self.empty, self.odd):
            for mode in MODES:
                with self.subTest(chain=os.path.basename(chain), mode=" ".join(mode)):
                    python = self._python(chain, mode)
                    program = self._run([str(harness.PROGRAM)], chain, mode)
                    self.assertEqual(program.returncode, python.returncode, program.stderr)
                    self.assertEqual(program.stderr, python.stderr)
                    self.assertEqual(program.stdout, python.stdout)


if __name__ == "__main__":
    unittest.main()
