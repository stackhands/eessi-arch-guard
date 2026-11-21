"""check-submit must read the line ends of eb-stack output as Python reads them.

The Python guard reads the output of eb-stack in text mode, so a carriage
return and a carriage return with a line feed both end a line and each
reaches the message as one line feed. The message then shows one space for
each of them. The lines of the standard output come from ``str.splitlines``,
which also ends a line at a vertical tab, a form feed, the file, group and
record separators, the next line character and the line and paragraph
separators. The Zig guard read the bytes as they were, so a pair of carriage
return and line feed showed as two spaces, and none of these characters
ended the line that names a flagged module.

A stub program takes the place of eb-stack, through the stub runner of the
shared runner module. The guard runs as ``<program> --check-submit --json
--target x86_64/amd/zen4 --eb-stack <stub>`` with a clean environment.

Python alone is checked for the messages it gives, and then the two programs
are compared. The comparison skips when TOOL_BIN is unset.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Dict, Iterator, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

PYTHON_ARGV = [sys.executable, "-I", str(harness.GUARD)]
LINE_ENDS: Dict[str, bytes] = {
    "carriage return": b"\r",
    "vertical tab": b"\x0b",
    "form feed": b"\x0c",
    "file separator": b"\x1c",
    "group separator": b"\x1d",
    "record separator": b"\x1e",
    "next line": "\u0085".encode("utf-8"),
    "line separator": "\u2028".encode("utf-8"),
    "paragraph separator": "\u2029".encode("utf-8"),
}
NOT_LINE_ENDS: Dict[str, bytes] = {
    "unit separator": b"\x1f",
    "single shift": "\u0084".encode("utf-8"),
    "hyphenation point": "\u2027".encode("utf-8"),
    "left to right embedding": "\u202a".encode("utf-8"),
    "lone lead byte": b"\xc2",
    "cut line separator": b"\xe2\x80",
}


def _failures() -> Iterator[Tuple[str, bytes, bytes, str]]:
    """Name, stderr and stdout of a failing stub, and the message Python gives."""
    yield "pair in stderr", b"first\r\nsecond\r\n", b"", "eb-stack failed: first second"
    yield "lone return in stderr", b"first\rsecond\r", b"", "eb-stack failed: first second"
    yield "line feed in stderr", b"first\nsecond\n", b"", "eb-stack failed: first second"
    yield "two pairs in stderr", b"a\r\n\r\nb", b"", "eb-stack failed: a  b"
    yield "pair then return in stderr", b"a\r\n\rb", b"", "eb-stack failed: a  b"
    yield "pair in stdout", b"", b"first\r\nsecond\r\n", "eb-stack failed: first second"
    yield "pair beside a stdout", b"err\r\nor\r\n", b"out\r\n", "eb-stack failed: err or"


class NewlineTests(unittest.TestCase):
    def test_python_shows_one_space_for_each_line_end(self) -> None:
        for name, stderr, stdout, message in _failures():
            with self.subTest(case=name):
                code, payload, err = harness.run_check_submit_stub(PYTHON_ARGV, stderr, stdout)
                self.assertEqual(code, 2, err)
                assert payload is not None
                self.assertEqual(payload["message"], message)
                self.assertEqual(err, ("eessi-arch-guard: check-submit: %s\n" % message).encode())

    def test_python_ends_a_flagged_line_at_each_line_end(self) -> None:
        for name, end in LINE_ENDS.items():
            with self.subTest(case=name):
                code, payload, err = harness.run_check_submit_stub(
                    PYTHON_ARGV, b"", b"noise" + end, 1, True
                )
                self.assertEqual(code, 1, err)
                assert payload is not None
                self.assertEqual(payload["missing"], ["R/4.4.2"])

    def test_python_keeps_a_line_across_characters_that_end_none(self) -> None:
        for name, other in NOT_LINE_ENDS.items():
            with self.subTest(case=name):
                code, _payload, err = harness.run_check_submit_stub(
                    PYTHON_ARGV, b"", b"noise" + other, 1, True
                )
                self.assertEqual(code, 2, err)

    def test_programs_agree(self) -> None:
        harness.skip_unless_tool_bin(self)
        program = [str(harness.PROGRAM)]
        for name, stderr, stdout, _message in _failures():
            with self.subTest(case=name):
                self.assertEqual(
                    harness.run_check_submit_stub(program, stderr, stdout),
                    harness.run_check_submit_stub(PYTHON_ARGV, stderr, stdout),
                )
        for name, other in {**LINE_ENDS, **NOT_LINE_ENDS}.items():
            with self.subTest(case="flagged line after " + name):
                self.assertEqual(
                    harness.run_check_submit_stub(program, b"", b"noise" + other, 1, True),
                    harness.run_check_submit_stub(PYTHON_ARGV, b"", b"noise" + other, 1, True),
                )


if __name__ == "__main__":
    unittest.main()
