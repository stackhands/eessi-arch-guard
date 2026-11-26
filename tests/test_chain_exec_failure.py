"""A chained prolog that cannot be started ends the run as a skip.

The guard starts the chain before it decides anything. When the exec fails,
with --json it prints a failed decision whose cause is the OS error class
and whose message is the error text with the program path in single quotes;
without --json it prints the skip line. The cases cover a chain whose
interpreter is missing, a chain that is not a program the kernel can run,
and a chain under a directory whose name has a quote and a character outside
ASCII, so that the message is quoted with the path escaped and the JSON is
escaped. Expected outputs are in ``golden/chain-exec-failure.txt``.
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
import retire_b_support as support  # noqa: E402

CPU = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0
MODES: Sequence[Sequence[str]] = (
    ("--json",),
    ("--json", "--strict"),
)


class ChainExecFailureTests(unittest.TestCase):
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

    def _run(self, chain: str, mode: Sequence[str]):
        env = harness.base_env(self.site, CPU, ["R/4.4.2"], gpu="none")
        argv = [*harness.GUARD_ARGV, "--chain", chain, *mode]
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT)

    def test_the_exception_text_is_the_message(self) -> None:
        proc = self._run(self.bad_shebang, ("--json",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["cause"], "FileNotFoundError")
        self.assertEqual(
            report["message"], "[Errno 2] No such file or directory: %r" % self.bad_shebang
        )
        self.assertIsNone(report["detail"])
        proc = self._run(self.empty, ("--json",))
        report = json.loads(proc.stdout)
        self.assertEqual(report["cause"], "OSError")
        self.assertEqual(report["message"], "[Errno 8] Exec format error: %r" % self.empty)

    def test_output_when_the_chain_cannot_be_started(self) -> None:
        sheet = support.sheet(self, "chain-exec-failure")
        for chain in (self.bad_shebang, self.empty, self.odd):
            for mode in MODES:
                name = "%s %s" % (os.path.basename(chain), " ".join(mode))
                if chain == self.odd:
                    name = "odd-directory " + name
                with self.subTest(case=name):
                    proc = self._run(chain, mode)
                    sheet.check(
                        self,
                        support.label(name),
                        support.render_proc(proc, support.site_work(self.site)),
                    )


if __name__ == "__main__":
    unittest.main()
