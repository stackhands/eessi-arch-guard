"""check-submit cuts an eb-stack failure down to its text.

When eb-stack fails without naming a flagged module, check-submit reports
the first of its stderr and its stdout that holds text, after removing every
Unicode whitespace character from both ends. A stderr that holds a no-break
space alone is empty text, so the stdout shows instead, and a message padded
with any whitespace character loses the padding.

The first test checks the message of each case. The second pins the exit
status, the JSON, and the standard error of each case in
``golden/check-submit-detail-text.txt``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Iterator, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import retire_b_support as support  # noqa: E402

SPACES = {
    "VT": "\x0b",
    "FF": "\x0c",
    "FS": "\x1c",
    "GS": "\x1d",
    "RS": "\x1e",
    "US": "\x1f",
    "NEL": "\u0085",
    "NBSP": "\u00a0",
    "OGHAM": "\u1680",
    "EN": "\u2003",
    "LS": "\u2028",
    "PS": "\u2029",
    "NNBSP": "\u202f",
    "MMSP": "\u205f",
    "IDEOGRAPHIC": "\u3000",
}


def _cases() -> Iterator[Tuple[str, str, str, str]]:
    """Name, stderr, stdout and the expected message."""
    for name, space in SPACES.items():
        yield "stderr of " + name + " alone", space, "out-text\n", "eb-stack failed: out-text"
        yield (
            "stderr padded by " + name,
            space + "msg" + space,
            "out-text\n",
            "eb-stack failed: msg",
        )
        yield "stdout padded by " + name, "", space + "msg" + space, "eb-stack failed: msg"
    yield "both empty", "", "", "eb-stack failed: exit 3"
    yield "stderr of ASCII space", " \t\r\n", "out-text", "eb-stack failed: out-text"


class SubmitDetailTests(unittest.TestCase):
    def test_the_message_is_the_text_that_trimming_leaves(self) -> None:
        for name, stderr, stdout, message in _cases():
            with self.subTest(case=name):
                code, payload, _err = harness.run_check_submit_stub(
                    harness.GUARD_ARGV, stderr.encode("utf-8"), stdout.encode("utf-8")
                )
                self.assertEqual(code, 2)
                assert payload is not None
                self.assertEqual(payload["message"], message)

    def test_output_of_each_case(self) -> None:
        sheet = support.sheet(self, "check-submit-detail-text")
        for name, stderr, stdout, _message in _cases():
            streams = (stderr.encode("utf-8"), stdout.encode("utf-8"))
            with self.subTest(case=name):
                result = harness.run_check_submit_stub(harness.GUARD_ARGV, *streams)
                sheet.check(self, support.label(name), support.render_stub(*result))


if __name__ == "__main__":
    unittest.main()
