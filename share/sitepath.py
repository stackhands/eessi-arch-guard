#!/usr/bin/env python3
"""Check that the EESSI client repository is a CVMFS mount and that the site
module path comes first.

This runs ``eessi-arch-guard --check-mount`` with the same arguments, so the
logic lives in the binary. Options: --repo, --site, --eessi, --modulepath,
--mountinfo, --require-input.
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
        sys.stderr.write("sitepath.py: eessi-arch-guard is not installed\n")
        return 2
    args = list(sys.argv[1:] if argv is None else argv)
    os.execv(binary, [binary, "--check-mount", *args])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
