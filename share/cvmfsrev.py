#!/usr/bin/env python3
"""Report a CVMFS client revision, and compare it only when asked.

`cvmfs_config stat` prints a line `Revision: N` (some builds say
`Catalog revision` or `Repository revision`). This reads that text from a
file. With no --min it prints the number and exits 0. It changes no mount.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys

_REVISION = re.compile(
    r"^(?:catalog revision|repository revision|revision)\s*:\s*(\d+)\s*$",
    re.IGNORECASE,
)
_STAT_READ_MAX = 1 << 20


def revision_of(text: str):
    """The first revision line in text, or None."""
    if not isinstance(text, str):
        return None
    for line in text.splitlines():
        match = _REVISION.match(line.strip())
        if match:
            return int(match.group(1))
    return None


def gate(found, minimum):
    """(exit, text). minimum None reports found. A smaller found is behind."""
    if found is None:
        return 2, "cvmfsrev.py: no revision in the stat output\n"
    if minimum is None:
        return 0, f"revision {found}\n"
    if found < minimum:
        return 1, f"behind revision {found} is older than {minimum}\n"
    return 0, f"revision {found}\n"


def _read_stat_bytes(path: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(0, "not a regular file")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > _STAT_READ_MAX:
                raise OSError(0, "file too large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cvmfsrev.py")
    parser.add_argument("--stat", required=True, help="file with cvmfs_config stat output")
    parser.add_argument("--min", type=int, default=None, help="fail when the client is older than this")
    args = parser.parse_args(argv)
    if args.min is not None and args.min < 0:
        sys.stderr.write("cvmfsrev.py: --min must be zero or more\n")
        return 2
    try:
        text = _read_stat_bytes(args.stat).decode("utf-8")
    except OSError as exc:
        sys.stderr.write(f"cvmfsrev.py: cannot read {args.stat}: {exc.strerror}\n")
        return 2
    code, message = gate(revision_of(text), args.min)
    if code == 0:
        sys.stdout.write(message)
    else:
        sys.stderr.write(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
