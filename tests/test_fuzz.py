"""Property checks: a value or a name cannot grow a second directive.

The values travel the whole way: an environment variable the fake Lmod
sets while it reloads the modules, through the delta, to the prolog or the
shell text. The prolog is read the way Slurm reads it, and the shell text
is evaluated by bash.
"""

from __future__ import annotations

import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
from slurm_model import apply_task_prolog  # noqa: E402

SEED = 20261101
ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"
HOSTILE = ["\n", "\r", "'", '"', "$", "`", "$(touch /tmp/x)", " ", ";", "|", "&", "\\"]


def random_text(rng: random.Random, n: int = 24) -> str:
    alphabet = "abcXYZ09/_-." + "".join(HOSTILE)
    return "".join(rng.choice(alphabet) for _ in range(n))


class FuzzTests(unittest.TestCase):
    def setUp(self):
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.site.write_cpuinfo("zen2")
        self.base = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")

    def run_with(self, payload: str, args=()):
        env = dict(self.base)
        env["EESSI_TEST_SENTINEL"] = payload
        return harness.run_guard(env, args)

    def test_hostile_exports_cannot_inject(self):
        rng = random.Random(SEED)
        payloads = [
            "a\nexport LD_PRELOAD=/evil.so",
            "a\r\nunset PATH",
            "$(touch /tmp/eessi-arch-guard-canary)",
            "`touch /tmp/eessi-arch-guard-canary`",
            "quote ' and \" and $HOME",
            "line1\nline2",
        ]
        for _ in range(80):
            payloads.append(random_text(rng))
        for payload in payloads:
            with self.subTest(payload=payload):
                proc = self.run_with(payload)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                changes, printed = apply_task_prolog(proc.stdout)
                for line in proc.stdout.split("\n"):
                    if line:
                        self.assertTrue(line.startswith(("export ", "unset ", "print ")), line)
                self.assertNotIn("LD_PRELOAD", changes)
                self.assertEqual(len(printed), 1)
                if any(char in payload for char in "\n\r"):
                    # A value that cannot be carried stops the whole delta.
                    self.assertEqual(set(changes), {"EESSI_ARCH_GUARD_FAILED"})
                else:
                    self.assertEqual(changes["ROUNDTRIP"], payload)
                    self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2)

    def test_shell_mode_does_not_execute_payloads(self):
        rng = random.Random(SEED + 1)
        canary_dir = tempfile.mkdtemp(prefix="eessi-arch-guard-fuzz-")
        self.addCleanup(shutil.rmtree, canary_dir, ignore_errors=True)
        values = [
            "$(touch %s/a)" % canary_dir,
            "`touch %s/b`" % canary_dir,
            "'; touch %s/c; '" % canary_dir,
            "a\nexport LD_PRELOAD=/evil",
        ]
        for _ in range(20):
            values.append(random_text(rng, 16))
        for value in values:
            with self.subTest(value=value):
                proc = self.run_with(value, ["--shell"])
                self.assertEqual(proc.returncode, 0, proc.stderr)
                check = subprocess.run(
                    [
                        "bash",
                        "--noprofile",
                        "--norc",
                        "-c",
                        'eval "$1"; printf %s "$ROUNDTRIP"; printf "\\0%s" "${LD_PRELOAD-unset}"',
                        "bash",
                        proc.stdout,
                    ],
                    capture_output=True,
                    env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
                    check=False,
                )
                self.assertEqual(check.returncode, 0, check.stderr)
                got, preload = check.stdout.split(b"\0", 1)
                # The reload script is read as text: each carriage return, with or
                # without a line feed after it, reaches the shell as one line feed.
                expected = value.replace("\r\n", "\n").replace("\r", "\n")
                self.assertEqual(got.decode("utf-8", "replace"), expected)
                self.assertEqual(preload, b"unset")
        self.assertEqual(os.listdir(canary_dir), [])

    def test_syslog_emergency_sanitizes_newline_subdir(self):
        with tempfile.TemporaryDirectory(prefix="eessi-arch-guard-em-") as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            (prefix / "software" / "linux" / "x86_64" / "amd" / "zen4" / "software").mkdir(
                parents=True
            )
            init = root / "init"
            init.mkdir()
            tmp_dir = root / "cache_tmp"
            tmp_dir.mkdir(mode=0o700)
            ad = init / "eessi_archdetect.sh"
            ad.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            ad.chmod(0o755)
            cpuinfo = root / "cpuinfo"
            cpuinfo.write_text("processor : 0\nflags : avx2 fma\n", encoding="utf-8")
            os.chmod(tmp_dir, 0)

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock_path = root / "em.sock"
            sock.bind(str(sock_path))
            sock.settimeout(2.0)
            try:
                env = {
                    "PATH": "/usr/bin:/bin",
                    "LC_ALL": "C",
                    "EESSI_SOFTWARE_SUBDIR": "a\nb",
                    "EESSI_PREFIX": str(prefix),
                    "EESSI_OS_TYPE": "linux",
                    "EESSI_INIT_PREFIX": str(init),
                    "EESSI_PROC_CPUINFO": str(cpuinfo),
                    "SLURM_JOB_ID": "123",
                    "TMPDIR": str(tmp_dir),
                }
                proc = harness.run_guard(
                    env, ["--timeout", "0.1", "--syslog", f"journal+unix://{sock_path}"]
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                pkt = sock.recv(65536)
                self.assertIn(b"ARCH_GUARD_WAS=unrecognized\n", pkt)
            finally:
                tmp_dir.chmod(0o700)
                sock.close()


if __name__ == "__main__":
    unittest.main()
