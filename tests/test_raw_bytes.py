"""The Python and Zig guards must agree byte for byte on input that is not UTF-8.

The other parity tests compare decoded text, which cannot hold input that is
not valid UTF-8 and would replace the bytes it cannot decode. These cases pass
such bytes through the environment and through the output of a stand-in for
eessi_archdetect.sh, run both programs with ``text`` off in
``harness.run_with_timeout``, and compare the raw bytes of exit status,
standard output, and standard error. The comparison skips when TOOL_BIN is
unset.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"
GENERIC = "x86_64/generic"
RUN_TIMEOUT = 20.0

ARCHDETECT_STUB = """#!/bin/bash
if [ "$1" = "-a" ] && [ "$2" = "cpupath" ]; then
    printf '%s' "$EESSI_TEST_CPUPATH"
    exit 0
fi
if [ "$1" = "accelpath" ]; then
    printf '%s' "$EESSI_TEST_ACCEL"
    exit 0
fi
exit 64
"""

GOOD_CPUPATH = ZEN2 + ":" + GENERIC
CASES = {
    "cpupath leaf with a stray byte": {"EESSI_TEST_CPUPATH": b"x86_64/amd/zen\xff"},
    "cpupath with a stray byte after a good leaf": {
        "EESSI_TEST_CPUPATH": ZEN2.encode() + b"\xff:" + GENERIC.encode()
    },
    "cpupath with an encoded surrogate": {"EESSI_TEST_CPUPATH": b"\xed\xa0\x80" + ZEN2.encode()},
    "cpupath with an overlong slash": {"EESSI_TEST_CPUPATH": ZEN2.encode() + b"\xc0\xaf"},
    "cpupath with a truncated sequence": {
        "EESSI_TEST_CPUPATH": GOOD_CPUPATH.encode() + b"\xe2\x82"
    },
    "accelerator with a stray byte": {"EESSI_TEST_ACCEL": b"accel/nvidia/cc\xff"},
    "override with a stray byte": {"EESSI_SOFTWARE_SUBDIR_OVERRIDE": b"x86_64/amd/zen\xff"},
    "loaded modules with a stray byte": {"LOADEDMODULES": b"EESSI/2025.06:R/4.4.\xff"},
    "temporary directory with a stray byte": {"TMPDIR": b"/tmp/\xff"},
    "search path with a stray byte": {"PATH": b"/usr/bin:/bin:/x\xff"},
    "module command with a stray byte": {"LMOD_CMD": b"/no/\xff"},
    "unrelated variable with a stray byte": {"EESSI_TEST_NOTE": b"a\xffb"},
    "unrelated variable with a latin-1 text": {"EESSI_TEST_NOTE": "caf\xe9".encode("latin-1")},
}
MODES = ((), ("--dry-run",), ("--dry-run", "--json"))


class RawBytesParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.site.write_cpuinfo("zen2")
        stub = Path(self.site.init) / "eessi_archdetect.sh"
        stub.write_text(ARCHDETECT_STUB, encoding="utf-8")
        stub.chmod(0o755)

    def _env(self, extra: Dict[str, bytes]) -> Dict[bytes, bytes]:
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        env["EESSI_TEST_CPUPATH"] = GOOD_CPUPATH
        raw = {os.fsencode(key): os.fsencode(value) for key, value in env.items()}
        raw.update({os.fsencode(key): value for key, value in extra.items()})
        return raw

    def _python(self, env: Dict[bytes, bytes], args):
        argv = [sys.executable, "-I", str(harness.GUARD), *args]
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT, text=False)

    def _program(self, env: Dict[bytes, bytes], args):
        argv = [str(harness.PROGRAM), *args]
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT, text=False)

    def test_runner_returns_bytes_that_are_not_utf8(self) -> None:
        env = self._env({"EESSI_TEST_NOTE": b"a\xffb"})
        proc = self._python(env, ("--dry-run", "--json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsInstance(proc.stdout, bytes)
        self.assertEqual(json.loads(proc.stdout)["kind"], "decision")
        with self.assertRaises(UnicodeDecodeError):
            harness.run_with_timeout(["/bin/printf", "\\377"], env={}, timeout=RUN_TIMEOUT)
        raw_run = harness.run_with_timeout(["/bin/printf", "\\377"], env={}, text=False)
        self.assertEqual(raw_run.stdout, b"\xff")

    def test_programs_agree_byte_for_byte(self) -> None:
        harness.skip_unless_tool_bin(self)
        outcomes = set()
        for label, extra in CASES.items():
            env = self._env(extra)
            for args in MODES:
                python = self._python(env, args)
                program = self._program(env, args)
                with self.subTest(case=label, args=args):
                    self.assertEqual(program.returncode, python.returncode)
                    self.assertEqual(program.stderr, python.stderr)
                    self.assertEqual(program.stdout, python.stdout)
                outcomes.add((python.returncode, python.stdout, python.stderr))
        self.assertGreaterEqual(len(outcomes), 4, "cases do not exercise different paths")


if __name__ == "__main__":
    unittest.main()
