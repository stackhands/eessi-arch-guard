"""check-submit takes a flagged module only on exit status 1.

When eb-stack exits with status 1 and names a loaded module as flagged on
standard output, check-submit reports the missing module even when eb-stack
also wrote a warning to standard error. When eb-stack exits with status 0 it
reports the loaded modules as available. On any other exit status a flagged
line on standard output does not make a missing-module result: check-submit
reports the failure of eb-stack, taking standard error ahead of standard
output.

Python alone is checked first, and then the two guards are compared through
the stub planner runner of the shared harness module. The comparison skips
when TOOL_BIN is unset.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

PYTHON_ARGV = [sys.executable, "-I", str(harness.GUARD)]
FAILED_LINE = b"eessi-arch-guard: check-submit: eb-stack failed: warn\n"


class CheckSubmitFlaggedStderrTests(unittest.TestCase):
    def test_status_one_reports_the_flagged_module_despite_stderr(self) -> None:
        code, payload, err = harness.run_check_submit_stub(
            PYTHON_ARGV, b"warn\n", b"head\n", status=1, flagged=True
        )
        absent_line = (
            f"eessi-arch-guard: check-submit: loaded module absent from "
            f"{harness.STUB_TARGET}: R/4.4.2\n"
        ).encode("utf-8")
        self.assertEqual(code, 1, err)
        assert payload is not None
        self.assertEqual(payload["exit"], 1)
        self.assertEqual(payload["exit_semantic"], "missing")
        self.assertEqual(payload["missing"], ["R/4.4.2"])
        self.assertIsNone(payload["message"])
        self.assertEqual(err, absent_line)

    def test_other_nonzero_statuses_report_stderr_even_when_flagged(self) -> None:
        for status in (2, 3):
            with self.subTest(status=status):
                code, payload, err = harness.run_check_submit_stub(
                    PYTHON_ARGV, b"warn\n", b"head\n", status=status, flagged=True
                )
                self.assertEqual(code, 2, err)
                assert payload is not None
                self.assertEqual(payload["exit"], 2)
                self.assertEqual(payload["exit_semantic"], "invalid")
                self.assertEqual(payload["missing"], [])
                self.assertEqual(payload["message"], "eb-stack failed: warn")
                self.assertEqual(err, FAILED_LINE)

    def test_status_zero_ignores_stderr_and_flagged_lines(self) -> None:
        code, payload, err = harness.run_check_submit_stub(
            PYTHON_ARGV, b"warn\n", b"head\n", status=0, flagged=True
        )
        self.assertEqual(code, 0, err)
        assert payload is not None
        self.assertEqual(payload["exit"], 0)
        self.assertEqual(payload["exit_semantic"], "ok")
        self.assertEqual(payload["missing"], [])
        self.assertEqual(
            payload["message"], f"1 loaded module(s) available for {harness.STUB_TARGET}"
        )
        self.assertEqual(err, b"")

    def test_programs_agree(self) -> None:
        harness.skip_unless_tool_bin(self)
        program = [str(harness.PROGRAM)]
        for status in (0, 1, 2, 3):
            with self.subTest(status=status, flagged=True):
                self.assertEqual(
                    harness.run_check_submit_stub(
                        program, b"warn\n", b"head\n", status=status, flagged=True
                    ),
                    harness.run_check_submit_stub(
                        PYTHON_ARGV, b"warn\n", b"head\n", status=status, flagged=True
                    ),
                )
        with self.subTest(status=1, flagged=False):
            self.assertEqual(
                harness.run_check_submit_stub(
                    program, b"warn\n", b"head\n", status=1, flagged=False
                ),
                harness.run_check_submit_stub(
                    PYTHON_ARGV, b"warn\n", b"head\n", status=1, flagged=False
                ),
            )


if __name__ == "__main__":
    unittest.main()
