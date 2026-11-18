#!/usr/bin/env python3
"""Report a CVMFS client revision, and compare it only when asked.

`cvmfs_config stat` prints a line `Revision: N` (some builds say
`Catalog revision` or `Repository revision`). This reads that text from a
file. With no --min it prints the number and exits 0. It changes no mount.
"""

from __future__ import annotations

import argparse
import re
import sys

_REVISION = re.compile(
    r"^(?:catalog revision|repository revision|revision)\s*:\s*(\d+)\s*$",
    re.IGNORECASE,
)


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cvmfsrev.py")
    parser.add_argument("--stat", required=True, help="file with cvmfs_config stat output")
    parser.add_argument("--min", type=int, default=None, help="fail when the client is older than this")
    args = parser.parse_args(argv)
    if args.min is not None and args.min < 0:
        sys.stderr.write("cvmfsrev.py: --min must be zero or more\n")
        return 2
    try:
        text = open(args.stat, encoding="utf-8").read()
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
