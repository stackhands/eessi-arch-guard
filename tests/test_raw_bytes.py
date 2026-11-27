"""The guard handles input that is not UTF-8 byte for byte.

Decoded text cannot hold input that is not valid UTF-8 and would replace the
bytes it cannot decode. These cases pass such bytes through the environment
and through the output of a stand-in for eessi_archdetect.sh, run the guard
with ``text`` off in ``harness.run_with_timeout``, and pin the raw bytes of
exit status, standard output, and standard error in ``golden/raw-bytes.txt``.
A byte outside printable ASCII shows in a sheet as a backslash escape. The
cases must end in at least four different outcomes.
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
import retire_b_support as support  # noqa: E402

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


class RawBytesTests(unittest.TestCase):
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

    def _program(self, env: Dict[bytes, bytes], args):
        argv = [*harness.GUARD_ARGV, *args]
        return harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT, text=False)

    def test_the_runner_returns_bytes_that_are_not_utf8(self) -> None:
        env = self._env({"EESSI_TEST_NOTE": b"a\xffb"})
        proc = self._program(env, ("--dry-run", "--json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsInstance(proc.stdout, bytes)
        self.assertEqual(json.loads(proc.stdout)["kind"], "decision")
        with self.assertRaises(UnicodeDecodeError):
            harness.run_with_timeout(["/bin/printf", "\\377"], env={}, timeout=RUN_TIMEOUT)
        raw_run = harness.run_with_timeout(["/bin/printf", "\\377"], env={}, text=False)
        self.assertEqual(raw_run.stdout, b"\xff")

    def test_output_byte_for_byte(self) -> None:
        sheet = support.sheet(self, "raw-bytes")
        outcomes = set()
        for label, extra in CASES.items():
            env = self._env(extra)
            for args in MODES:
                proc = self._program(env, args)
                name = "%s %s" % (label, " ".join(args) or "live")
                with self.subTest(case=label, args=args):
                    support.check(self, sheet, name, proc, self.site)
                outcomes.add((proc.returncode, proc.stdout, proc.stderr))
        self.assertGreaterEqual(len(outcomes), 4, "cases do not exercise different paths")


if __name__ == "__main__":
    unittest.main()
