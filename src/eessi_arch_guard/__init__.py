"""A thin launcher for the eessi-arch-guard binary.

The guard is the static binary. The wheel for a platform carries it in
``eessi_arch_guard/bin``; ``main`` runs it with the arguments it was given,
replacing this process through the ``_launch`` extension (``execv(2)``), so
Slurm sees the binary and nothing else.
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from . import _launch
from ._locate import NAME, bundled, locate, on_path

__version__ = "0.3.1"
__all__ = ["NAME", "__version__", "bundled", "locate", "main", "on_path"]


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the binary with ``argv`` (default: ``sys.argv[1:]``); does not return."""
    args = list(sys.argv[1:] if argv is None else argv)
    binary = locate()
    if binary is None:
        sys.stderr.write(
            "eessi-arch-guard: no binary found. The wheel for this platform bundles one; "
            "otherwise put a release binary named eessi-arch-guard on PATH.\n"
        )
        return 127
    try:
        _launch.execv(binary, [binary, *args])
    except OSError as exc:
        sys.stderr.write("eessi-arch-guard: cannot run %s: %s\n" % (binary, exc.strerror))
        return 126
    return 0
