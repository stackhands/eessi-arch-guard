"""check-submit survives an eb-stack that writes bytes that are not UTF-8.

When eb-stack fails, check-submit reports the text of its stderr or its
stdout. An invalid byte does not end the run: the failure of eb-stack is
reported with exit status 2, the JSON message keeps the byte as one escaped
code point (a lone surrogate, U+DC00 plus the byte), and the line on
standard error shows a question mark for it.

A stub program stands in for eb-stack. The test writes it as an executable
script in a tree of its own, and the stub writes the bytes it was given to
its two streams and exits with the status it was given. The guard runs as
``<program> --check-submit --json --target x86_64/amd/zen4 --eb-stack
<stub>`` with a clean environment. The cases run with and without
PYTHONIOENCODING in the environment, which the guard ignores. The explicit
expectations come first, and ``golden/check-submit-non-utf8.txt`` pins the
whole output of each case.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Dict, Iterator, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import retire_b_support as support  # noqa: E402

PROGRAM = harness.GUARD_ARGV
STREAM_SETTINGS: Dict[str, Dict[str, str]] = {
    "default": {},
    "strict": {"PYTHONIOENCODING": "utf-8:strict"},
}


def _cases() -> Iterator[Tuple[str, bytes, bytes, int, str, str]]:
    """Name, stderr, stdout, exit status of the stub, message and stderr line."""
    prefix = "eessi-arch-guard: check-submit: "
    yield (
        "invalid byte in stderr",
        b"bad \xff byte\n",
        b"",
        3,
        "eb-stack failed: bad \udcff byte",
        prefix + "eb-stack failed: bad ? byte\n",
    )
    yield (
        "invalid byte alone in stderr",
        b"\xff",
        b"out-text\n",
        3,
        "eb-stack failed: \udcff",
        prefix + "eb-stack failed: ?\n",
    )
    yield (
        "cut multibyte character in stderr",
        b"caf\xc3",
        b"",
        3,
        "eb-stack failed: caf\udcc3",
        prefix + "eb-stack failed: caf?\n",
    )
    yield (
        "invalid byte in stdout",
        b"",
        b"\xfe out\n",
        3,
        "eb-stack failed: \udcfe out",
        prefix + "eb-stack failed: ? out\n",
    )
    yield (
        "valid text in stderr",
        "caf\u00e9\n".encode("utf-8"),
        b"\xfe",
        3,
        "eb-stack failed: caf\u00e9",
        prefix + "eb-stack failed: caf\u00e9\n",
    )


class NonUtf8Tests(unittest.TestCase):
    def test_the_failure_of_eb_stack_is_reported(self) -> None:
        for setting, streams in STREAM_SETTINGS.items():
            for name, stderr, stdout, status, message, line in _cases():
                with self.subTest(streams=setting, case=name):
                    code, payload, err = harness.run_check_submit_stub(
                        PROGRAM, stderr, stdout, status, extra_env=streams
                    )
                    self.assertEqual(code, 2, err)
                    assert payload is not None
                    self.assertEqual(payload["message"], message)
                    self.assertEqual(err, line.encode("utf-8"))

    def test_the_module_is_named_when_stdout_has_an_invalid_line(self) -> None:
        for setting, streams in STREAM_SETTINGS.items():
            with self.subTest(streams=setting):
                code, payload, err = harness.run_check_submit_stub(
                    PROGRAM, b"", b"\xff junk\n", 1, True, streams
                )
                self.assertEqual(code, 1, err)
                assert payload is not None
                self.assertEqual(payload["missing"], ["R/4.4.2"])

    def test_output_of_each_case(self) -> None:
        sheet = support.sheet(self, "check-submit-non-utf8")
        run = harness.run_check_submit_stub
        for setting, streams in STREAM_SETTINGS.items():
            for name, stderr, stdout, status, _message, _line in _cases():
                label = "%s: %s" % (setting, name)
                with self.subTest(case=label):
                    result = run(PROGRAM, stderr, stdout, status, extra_env=streams)
                    sheet.check(self, support.label(label), support.render_stub(*result))
            label = "%s: invalid line beside a flagged module" % setting
            with self.subTest(case=label):
                result = run(PROGRAM, b"", b"\xff junk\n", 1, True, streams)
                sheet.check(self, support.label(label), support.render_stub(*result))


if __name__ == "__main__":
    unittest.main()
