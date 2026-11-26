"""Helpers that turn the runs of a test into text for a golden sheet.

``golden.py`` reads a sheet with universal newlines and splits it with
``str.splitlines``, so a carriage return, a vertical tab or a Unicode line
separator in a stored output would not come back as written. ``escape``
writes every character outside printable ASCII and the line feed as a
backslash escape, which keeps a sheet ASCII and every output distinct.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from typing import Sequence

import golden

_STUB_DIRECTORY = re.compile(r"[^\s\"']*eag-stub-[A-Za-z0-9_]+")


def escape(text: str, keep_newline: bool = True) -> str:
    """``text`` with each character outside printable ASCII written as an escape."""
    out = []
    for ch in text:
        code = ord(ch)
        if 0x20 <= code <= 0x7E or (ch == "\n" and keep_newline):
            out.append(ch)
        elif code < 0x100:
            out.append("\\x%02x" % code)
        elif code < 0x10000:
            out.append("\\u%04x" % code)
        else:
            out.append("\\U%08x" % code)
    return "".join(out)


def label(text: str) -> str:
    """``text`` as the label of a golden section: one line of printable ASCII."""
    return escape(text, keep_newline=False)


def render(returncode: int, stdout: str, stderr: str, work: Sequence[str] = ()) -> str:
    """``golden.render`` of the escaped streams."""
    return golden.render(returncode, escape(stdout), escape(stderr), work=work)


def render_proc(proc, work: Sequence[str] = ()) -> str:
    """The exit status and streams of a finished run, as text or as bytes."""
    out, err = proc.stdout, proc.stderr
    if isinstance(out, bytes):
        out = out.decode("utf-8", "backslashreplace")
    if isinstance(err, bytes):
        err = err.decode("utf-8", "backslashreplace")
    return render(proc.returncode, out, err, work)


def render_stub(code: int, payload, err: bytes) -> str:
    """The result of ``harness.run_check_submit_stub`` as text."""
    body = "" if payload is None else json.dumps(payload, sort_keys=True, ensure_ascii=True)
    text = render(code, body, err.decode("utf-8", "backslashreplace"))
    return _STUB_DIRECTORY.sub("@STUB@", text)


def sheet(case: unittest.TestCase, name: str) -> golden.Sheet:
    """A golden sheet that is finished when ``case`` ends."""
    result = golden.Sheet(name)
    case.addCleanup(result.finish)
    return result


def site_work(site) -> Sequence[str]:
    """The directories of ``site`` that a golden sheet replaces with ``@WORK@``."""
    return [site.root, os.path.realpath(site.root)]


def check(case: unittest.TestCase, sheet_: golden.Sheet, name: str, proc, site) -> None:
    """Check the run ``proc`` on ``site`` against the section ``name`` of ``sheet_``."""
    sheet_.check(case, label(name), render_proc(proc, site_work(site)))
