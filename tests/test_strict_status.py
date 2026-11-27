"""Opt-in strict status at the public shell and task-prolog interfaces."""

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402

STRICT_FAILURE_EXIT = 78
ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"


class StrictStatusTests(unittest.TestCase):
    def setUp(self):
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.site.write_cpuinfo("zen2")

    def env(self, loaded=ZEN4, **extra):
        env = harness.base_env(self.site, loaded, ["R/4.4.2"], gpu="none")
        env.update(extra)
        return env

    def failing_env(self):
        return self.env(LOADEDMODULES="EESSI/2025.06:R/4.4.2\nexport LD_PRELOAD=x")

    def hang_env(self):
        self.site.write_cpuinfo("zen4")
        pid_file = os.path.join(self.site.root, "hang.pid")
        return self.env(
            EESSI_TEST_GPU="1",
            EESSI_TEST_GPU_HANG="30",
            EESSI_TEST_HANG_PID=pid_file,
        )

    def test_failed_decision_status_follows_strict_and_the_mode(self):
        # strict flag, output mode, expected status
        rows = (
            (["--strict"], [], STRICT_FAILURE_EXIT),
            (["--strict"], ["--shell"], STRICT_FAILURE_EXIT),
            ([], [], 0),
            ([], ["--shell"], 0),
        )
        for strict, mode, code in rows:
            with self.subTest(strict=strict, mode=mode):
                proc = harness.run_guard(self.failing_env(), [*strict, *mode])
                self.assertEqual(proc.returncode, code, proc.stderr)
                self.assertIn("EESSI_ARCH_GUARD_FAILED", proc.stdout)

    def test_interrupted_decision_is_strict_in_each_output_mode(self):
        for mode in ([], ["--shell"]):
            for strict, code in ((["--strict"], 78), ([], 0)):
                with self.subTest(mode=mode, strict=strict):
                    env = self.hang_env()
                    pid_file = Path(env["EESSI_TEST_HANG_PID"])
                    if pid_file.exists():
                        pid_file.unlink()
                    child = subprocess.Popen(
                        [*harness.GUARD_ARGV, *strict, *mode],
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        encoding="utf-8",
                    )
                    for _ in range(200):
                        if pid_file.is_file() and pid_file.read_text(encoding="ascii").strip():
                            break
                        time.sleep(0.05)
                    child.send_signal(signal.SIGINT)
                    out, err = child.communicate(timeout=20)
                    self.assertEqual(child.returncode, code, err)
                    self.assertIn("skipped", out + err)

    def test_successful_and_intentional_noop_decisions_remain_zero(self):
        cases = {
            "reloaded": self.env(),
            "noop": self.env(loaded=ZEN2),
            "skip": self.env(EESSI_ARCH_GUARD="0"),
        }
        for kind, env in cases.items():
            with self.subTest(kind=kind):
                proc = harness.run_guard(env, ["--strict"])
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertNotIn("EESSI_ARCH_GUARD_FAILED", proc.stdout)
        reloaded = harness.run_guard(cases["reloaded"], ["--strict"])
        self.assertIn("EESSI_ARCH_GUARD_RELOADED", reloaded.stdout)

    def test_chained_prolog_failure_preserves_its_status(self):
        chain = os.path.join(self.site.root, "user-prolog")
        with open(chain, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho 'print chain stopped'\nexit 17\n")
        os.chmod(chain, 0o755)
        proc = harness.run_guard(self.env(), ["--strict", "--chain", chain])
        self.assertEqual(proc.returncode, 17, proc.stderr)
        self.assertIn("print chain stopped", proc.stdout)
        self.assertEqual(harness.log_lines(self.site.log), [])


class ArchdetectRefusalTests(unittest.TestCase):
    def test_invalid_cpupath_and_accelpath_outputs_fail_and_honor_strict(self):
        zen2 = "x86_64/amd/zen2"
        zen4 = "x86_64/amd/zen4"
        cases = (
            (
                "multiline-cpupath",
                "if [ \"$2\" = cpupath ]; then "
                "printf 'x86_64/amd/zen2\\nx86_64/generic\\n'; fi\n",
                "cpupath output is not a single line",
            ),
            (
                "unsafe-cpupath",
                "if [ \"$2\" = cpupath ]; then echo '../bad'; fi\n",
                "cpupath returned an unsafe candidate",
            ),
            (
                "bad-accelpath",
                "if [ \"$2\" = cpupath ]; then echo x86_64/amd/zen2; "
                "else echo 'not/an/accel'; fi\n",
                "accelpath output is not an accelerator subdirectory",
            ),
        )
        for name, script_body, expected_reason in cases:
            for args, expected_code in (([], 0), (["--strict"], STRICT_FAILURE_EXIT)):
                with self.subTest(case=name, args=args):
                    site = harness.Site()
                    try:
                        site.add_cpu(zen2)
                        site.add_cpu(zen4)
                        site.write_cpuinfo("zen2")
                        ad_path = Path(site.init) / "eessi_archdetect.sh"
                        ad_path.write_text("#!/bin/sh\n" + script_body, encoding="utf-8")
                        ad_path.chmod(0o755)
                        env = harness.base_env(site, zen4, ["R/4.4.2"], gpu="none")
                        proc = harness.run_guard(env, args)
                        self.assertEqual(proc.returncode, expected_code, proc.stderr)
                        self.assertEqual(proc.stderr, "")
                        self.assertIn("->archdetect-failed", proc.stdout)
                        self.assertIn(expected_reason, proc.stdout)
                        self.assertIn("environment left as it was", proc.stdout)
                    finally:
                        site.close()
