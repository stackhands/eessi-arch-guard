"""Property checks: module names and prolog lines cannot grow a second directive."""

from __future__ import annotations

import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import eessi_arch_guard as guard  # noqa: E402
import harness  # noqa: E402
from slurm_model import apply_task_prolog  # noqa: E402

SEED = 20261101
HOSTILE = ["\n", "\r", "'", '"', "$", "`", "$(touch /tmp/x)", " ", ";", "|", "&", "\\", "\0"]


def _slurm_safe(text: str, allowed: dict) -> None:
    changes, _printed = apply_task_prolog(text)
    assert set(changes) <= set(allowed)
    for key, value in changes.items():
        assert guard.ENV_NAME_RE.fullmatch(key), key
        assert "\n" not in key and "\r" not in key
        if value is not None:
            assert value == allowed[key]
            assert "\n" not in value and "\r" not in value
    for line in text.split("\n"):
        if line == "":
            continue
        assert line.startswith(("export ", "unset ", "print ")), line
        # A second directive would already have been split out by the newline.


def _random_text(rng: random.Random, n: int = 24) -> str:
    alphabet = "abcXYZ09/_-." + "".join(HOSTILE)
    return "".join(rng.choice(alphabet) for _ in range(n))


class FuzzTests:
    def test_hostile_module_names_are_rejected(self):
        rng = random.Random(SEED)
        assert guard.module_name_ok("EESSI/2025.06")
        assert guard.module_name_ok("foss/2024a")
        for _ in range(300):
            body = "R/4.4.2"
            cut = rng.randrange(len(body) + 1)
            name = body[:cut] + rng.choice(HOSTILE) + body[cut:]
            assert not guard.module_name_ok(name), repr(name)
            loaded = guard.parse_loaded_modules({"LOADEDMODULES": "EESSI/2025.06:" + name})
            assert loaded.error == "unsafe-module-name"

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
            payloads.append(_random_text(rng))
        for payload in payloads:
            old = {"PATH": "/usr/bin", "KEEP": "same"}
            new = {"PATH": "/usr/bin", "KEEP": "same", "FOO": payload, "OK": "yes"}
            # A newline buried in the name, which the prototype would have printed.
            new["BAD\nexport LD_PRELOAD"] = "1"
            delta = guard.environment_delta(old, new, multiline=False)
            assert delta is None
            # Drop the illegal name. A newline in the value still aborts.
            del new["BAD\nexport LD_PRELOAD"]
            delta = guard.environment_delta(old, new, multiline=False)
            if any(char in payload for char in "\n\r\0"):
                assert delta is None
                continue
            assert delta is not None
            text = os.fsdecode(
                guard.render_bytes(delta, "note\nexport PWNED=1", shell=False, announce=True)
            )
            _slurm_safe(text, delta)
            changes, printed = apply_task_prolog(text)
            assert "PWNED" not in changes
            assert "LD_PRELOAD" not in changes
            assert printed == ["note export PWNED=1"]

    def test_shell_mode_does_not_execute_payloads(self):
        rng = random.Random(SEED + 1)
        canary_dir = tempfile.mkdtemp(prefix="eessi-arch-guard-fuzz-")
        try:
            values = [
                "$(touch %s/a)" % canary_dir,
                "`touch %s/b`" % canary_dir,
                "'; touch %s/c; '" % canary_dir,
                "a\nexport LD_PRELOAD=/evil",
            ]
            for _ in range(20):
                values.append(_random_text(rng, 16).replace("\0", ""))
            for value in values:
                text = os.fsdecode(
                    guard.render_bytes({"FOO": value}, None, shell=True, announce=False)
                )
                proc = subprocess.run(
                    [
                        "bash",
                        "--noprofile",
                        "--norc",
                        "-c",
                        'eval "$1"; printf %s "$FOO"',
                        "bash",
                        text,
                    ],
                    capture_output=True,
                    env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
                    check=False,
                )
                assert proc.returncode == 0, proc.stderr
                assert proc.stdout.decode("utf-8", "replace") == value
            leftover = os.listdir(canary_dir)
            assert leftover == [], leftover
        finally:
            shutil.rmtree(canary_dir, ignore_errors=True)

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
                assert proc.returncode == 0, proc.stderr
                pkt = sock.recv(65536)
                assert b"ARCH_GUARD_WAS=unrecognized\n" in pkt, pkt
            finally:
                tmp_dir.chmod(0o700)
                sock.close()


def load_tests(_loader, _tests, _pattern):
    import unittest

    suite = unittest.TestSuite()
    case = FuzzTests()
    for name in sorted(dir(case)):
        if name.startswith("test_"):
            suite.addTest(unittest.FunctionTestCase(getattr(case, name), description=name))
    return suite
