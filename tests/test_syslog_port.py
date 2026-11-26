"""The Python and Zig guards must read syslog ports as ASCII digits only.

``str.isdigit`` accepts digits from other scripts and superscript digits, so a
port written in them passes the Python check. ``int`` then accepts the
script digits and raises on the superscripts, which ended the guard decision
with an exception. The cases run both programs on one generated tree and
compare exit status, standard output, and standard error. The comparison
skips when TOOL_BIN is unset.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"
RUN_TIMEOUT = 20.0

ACCEPTED = {
    "udp://h:80": "udp://h:80",
    "udp://h:0080": "udp://h:80",
    "tcp://h:65535": "tcp://h:65535",
    "udp://[::1]:514": "udp://[::1]:514",
}
REJECTED = (
    "udp://h:65536",
    "udp://h:0",
    "udp://h:\uff18\uff10",
    "udp://h:\u0668\u0660",
    "udp://h:\u09ea\u09e8",
    "udp://h:\u00b2",
    "udp://h:\u2461",
    "udp://h:\u00b2\u00b3",
    "tcp://[::1]:\uff15\uff11\uff14",
    "udp://h:5\u0661\u0664",
)
TARGETS = (*ACCEPTED, *REJECTED)


class SyslogPortParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.site.write_cpuinfo("zen2")

    def _env(self, target: Optional[str] = None) -> Dict[str, str]:
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        if target is not None:
            env["EESSI_ARCH_GUARD_SYSLOG"] = target
        return env

    def _python(self, env: Dict[str, str], *args: str):
        argv = [sys.executable, "-I", str(harness.GUARD), "--dry-run", "--json", *args]
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT)

    def _program(self, env: Dict[str, str], *args: str):
        argv = [str(harness.PROGRAM), "--dry-run", "--json", *args]
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT)

    def test_python_guard_accepts_only_ascii_digit_ports(self) -> None:
        for target in TARGETS:
            with self.subTest(target=target):
                proc = self._python(self._env(), "--syslog", target)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                doc = json.loads(proc.stdout)
                if target in ACCEPTED:
                    self.assertEqual(doc["syslog"]["target"], ACCEPTED[target])
                    self.assertEqual(proc.stderr, "")
                else:
                    self.assertIsNone(doc["syslog"]["target"])
                    self.assertIn("not a valid syslog target", proc.stderr)
                    self.assertNotIn("Traceback", proc.stderr)

    def test_programs_agree_on_ports(self) -> None:
        harness.skip_unless_tool_bin(self)
        for target in TARGETS:
            for label, args, env in (
                ("option", ("--syslog", target), self._env()),
                ("variable", (), self._env(target)),
            ):
                python = self._python(env, *args)
                program = self._program(env, *args)
                with self.subTest(target=target, via=label):
                    self.assertEqual(program.returncode, python.returncode)
                    self.assertEqual(program.stderr, python.stderr)
                    self.assertEqual(program.stdout, python.stdout)


if __name__ == "__main__":
    unittest.main()
