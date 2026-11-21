"""check-submit must survive an eb-stack that writes bytes that are not UTF-8.

When eb-stack fails, check-submit reports the text of its stderr or its
stdout. The Python guard read both streams in text mode, which decodes with
strict errors, so one byte that is not UTF-8 ended the run with a
UnicodeDecodeError that main turned into a failed decision with exit status
0 instead of the failure of eb-stack with exit status 2. The Zig guard reads
the bytes, keeps an invalid byte as one character and writes a question mark
for it on standard error.

A stub program stands in for eb-stack. The test writes it as an executable
script in a tree of its own, and the stub writes the bytes it was given to
its two streams and exits with the status it was given. The guard runs as
``<program> --check-submit --json --target x86_64/amd/zen4 --eb-stack
<stub>`` with a clean environment. The Python guard also runs with strict
error handling on its own streams, through PYTHONIOENCODING, which cannot be
set in isolated mode, so the Python runs do not use that mode.

Python alone is checked for the messages it gives, in two stream settings,
and then the two programs are compared. The comparison skips when TOOL_BIN is
unset.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Dict, Iterator, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

PYTHON_ARGV = [sys.executable, str(harness.GUARD)]
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
    def test_python_reports_the_failure_of_eb_stack(self) -> None:
        for setting, streams in STREAM_SETTINGS.items():
            for name, stderr, stdout, status, message, line in _cases():
                with self.subTest(streams=setting, case=name):
                    code, payload, err = harness.run_check_submit_stub(
                        PYTHON_ARGV, stderr, stdout, status, extra_env=streams
                    )
                    self.assertEqual(code, 2, err)
                    assert payload is not None
                    self.assertEqual(payload["message"], message)
                    self.assertEqual(err, line.encode("utf-8"))

    def test_python_names_the_module_when_stdout_has_an_invalid_line(self) -> None:
        for setting, streams in STREAM_SETTINGS.items():
            with self.subTest(streams=setting):
                code, payload, err = harness.run_check_submit_stub(
                    PYTHON_ARGV, b"", b"\xff junk\n", 1, True, streams
                )
                self.assertEqual(code, 1, err)
                assert payload is not None
                self.assertEqual(payload["missing"], ["R/4.4.2"])

    def test_programs_agree(self) -> None:
        harness.skip_unless_tool_bin(self)
        program = [str(harness.PROGRAM)]
        run = harness.run_check_submit_stub
        for setting, streams in STREAM_SETTINGS.items():
            for name, stderr, stdout, status, _message, _line in _cases():
                with self.subTest(streams=setting, case=name):
                    self.assertEqual(
                        run(program, stderr, stdout, status, extra_env=streams),
                        run(PYTHON_ARGV, stderr, stdout, status, extra_env=streams),
                    )
            with self.subTest(streams=setting, case="invalid line beside a flagged module"):
                self.assertEqual(
                    run(program, b"", b"\xff junk\n", 1, True, streams),
                    run(PYTHON_ARGV, b"", b"\xff junk\n", 1, True, streams),
                )


if __name__ == "__main__":
    unittest.main()
