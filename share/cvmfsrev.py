#!/usr/bin/env python3
"""Report a CVMFS client revision, and compare it only when asked.

This runs ``eessi-arch-guard --check-revision`` with the same arguments, so the
logic lives in the binary. ``--stat FILE`` names the output of ``cvmfs_config
stat``; ``--min N`` fails when the client is older than N.
"""

import os
import sys


def _is_binary(path):
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    with open(path, "rb") as handle:
        return handle.read(4) == b"\x7fELF"


def _binary():
    try:
        from eessi_arch_guard import locate
    except ImportError:
        locate = None
    found = locate() if locate else None
    if found:
        return found
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, "eessi-arch-guard")
        if _is_binary(candidate):
            return candidate
    return None


def main(argv=None):
    binary = _binary()
    if binary is None:
        sys.stderr.write("cvmfsrev.py: eessi-arch-guard is not installed\n")
        return 2
    args = list(sys.argv[1:] if argv is None else argv)
    os.execv(binary, [binary, "--check-revision", *args])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
