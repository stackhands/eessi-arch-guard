"""Expected-output files for the tests that pin a program's whole output.

A golden file ``tests/golden/NAME.txt`` holds one section per case. A section
starts with a line ``=== LABEL`` and runs to the next one. A test makes a
``Sheet``, checks each case against it, and calls ``finish``::

    sheet = golden.Sheet("cpuinfo-tempdir")
    self.addCleanup(sheet.finish)
    sheet.check(self, "json limit 0", golden.render(run, work=run.site_root))

Temporary directories have random names, so a case is compared after the
placeholders of ``normalize`` replace them: ``@WORK@`` for the directory of
the run and ``@REPO@`` for the checkout. ``RECORD=1`` rewrites the files from
the output of the program under test instead of comparing; read the diff of a
re-recorded file before committing it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional, Sequence
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = Path(__file__).resolve().parent / "golden"
RECORD = os.environ.get("RECORD") == "1"
_HEADER = re.compile(r"^=== (.*)$")
_RANDOM = re.compile(r"eessi-arch-guard-(?:cpu-)?[a-z0-9_]{8}")


def normalize(text: str, work: Sequence[str] = ()) -> str:
    """``text`` with run-specific paths replaced by placeholders.

    Each directory of ``work`` becomes ``@WORK@``, the checkout becomes
    ``@REPO@``, and a random workspace name becomes ``RND``.
    """
    for directory in sorted((w for w in work if w), key=len, reverse=True):
        text = text.replace(directory, "@WORK@")
    text = text.replace(str(ROOT), "@REPO@")
    return _RANDOM.sub("RND", text)


def render(returncode: int, stdout: str, stderr: str, work: Sequence[str] = ()) -> str:
    """The exit status and both streams of a run, normalized, as one text.

    Each stream is followed by a line feed of its own, so a stream that does
    not end in one stays distinguishable from one that does.
    """
    text = "rc: %d\n--- stdout\n%s\n--- stderr\n%s\n--- end\n" % (returncode, stdout, stderr)
    return normalize(text, work)


def _parse(path: Path) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    label: Optional[str] = None
    lines = []
    with open(path, encoding="utf-8", newline="") as handle:
        text = handle.read()
    for line in [x + "\n" for x in text.split("\n")[:-1]]:
        match = _HEADER.match(line[:-1])
        if match:
            if label is not None:
                sections[label] = "".join(lines)
            label, lines = match.group(1), []
        else:
            lines.append(line)
    if label is not None:
        sections[label] = "".join(lines)
    return sections


class Sheet:
    """The cases of one golden file."""

    def __init__(self, name: str) -> None:
        self.path = DIRECTORY / (name + ".txt")
        self.recorded: Dict[str, str] = {}
        self.checked = set()
        self.expected = {} if RECORD or not self.path.exists() else _parse(self.path)

    def check(self, case: TestCase, label: str, actual: str) -> None:
        """Compare ``actual`` with the section ``label``, or record it."""
        self.checked.add(label)
        case.maxDiff = None
        if RECORD:
            self.recorded[label] = actual
            return
        case.assertIn(label, self.expected, "no golden section %r in %s" % (label, self.path))
        case.assertEqual(
            actual, self.expected[label], "golden section %r of %s" % (label, self.path)
        )

    def finish(self) -> None:
        """Write the recorded cases, or note a section no case checked."""
        if RECORD:
            DIRECTORY.mkdir(exist_ok=True)
            body = "".join("=== %s\n%s" % (k, v) for k, v in self.recorded.items())
            with open(self.path, "w", encoding="utf-8", newline="") as handle:
                handle.write(body)
            return
        stale = sorted(set(self.expected) - self.checked)
        if stale and self.checked:
            raise AssertionError("golden sections that no case checked: %r" % (stale,))
