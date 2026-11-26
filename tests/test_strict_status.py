"""Opt-in strict status at the public shell and task-prolog interfaces."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import eessi_arch_guard as guard  # noqa: E402
import harness  # noqa: E402


class StrictStatusTests(unittest.TestCase):
    def run_main(self, flags, outcome=None, error=None):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(guard, "site_config", return_value=({}, None)),
            mock.patch.object(guard, "limited_decide", return_value=outcome, side_effect=error),
            mock.patch.object(guard, "write_fd") as output,
        ):
            code = guard.main(flags)
        return code, b"".join(call.args[1] for call in output.call_args_list if call.args[0] == 1)

    def test_strict_status_follows_the_failure_and_the_mode(self):
        failed = guard.Outcome(
            {"EESSI_ARCH_GUARD_FAILED": "old->restore-failed"}, "refused", "failed"
        )
        boom = RuntimeError("decision failed")
        # strict, outcome, error, mode, expect code, check the failure text
        rows = (
            (True, failed, None, [], 78, True),
            (True, failed, None, ["--shell"], 78, True),
            (False, failed, None, [], 0, True),
            (False, failed, None, ["--shell"], 0, True),
            (True, None, boom, [], 78, False),
            (True, None, boom, ["--shell"], 78, False),
            (False, None, boom, [], 0, False),
        )
        for strict, outcome, error, mode, expect, check_text in rows:
            flags = (["--strict"] if strict else []) + list(mode)
            with self.subTest(strict=strict, mode=mode, error=error is not None):
                code, text = self.run_main(flags, outcome, error)
                self.assertEqual(code, expect)
                if check_text:
                    self.assertIn(b"EESSI_ARCH_GUARD_FAILED", text)

    def test_outer_startup_exception_is_strict(self):
        with (
            mock.patch.object(guard, "site_config", side_effect=RuntimeError("startup failed")),
            mock.patch.object(guard, "write_fd"),
        ):
            self.assertEqual(guard.main(["--strict", "--shell"]), 78)
            self.assertEqual(guard.main(["--shell"]), 0)

    def test_interrupted_decision_is_strict(self):
        code, _text = self.run_main(["--strict", "--shell"], error=KeyboardInterrupt())
        self.assertEqual(code, 78)

    def test_successful_and_intentional_noop_decisions_remain_zero(self):
        for kind in ("skip", "noop", "reloaded"):
            with self.subTest(kind=kind):
                code, _text = self.run_main(["--strict"], guard.Outcome({}, None, kind))
                self.assertEqual(code, 0)

    def test_chained_prolog_failure_preserves_its_status(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(guard, "site_config", return_value=({}, None)),
            mock.patch.object(guard, "_chain_path_ok", return_value=True),
            mock.patch.object(guard, "run_chain", return_value=(17, b"print chain stopped\n", b"")),
            mock.patch.object(guard, "limited_decide") as decision,
            mock.patch.object(guard, "write_fd"),
        ):
            self.assertEqual(guard.main(["--strict", "--chain", "/site/user-prolog"]), 17)
            decision.assert_not_called()


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
            for args, expected_code in (([], 0), (["--strict"], guard.STRICT_FAILURE_EXIT)):
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
