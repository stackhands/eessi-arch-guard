"""Find the eessi-arch-guard binary. An empty PATH entry (the current directory) is skipped."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

NAME = "eessi-arch-guard"
_ELF = b"\x7fELF"


def _is_binary(path: Optional[str]) -> bool:
    """True for an executable file that starts with the ELF magic.

    A script of the same name, such as the console script pip generates, is
    not the binary, and running it from here would start this module again.
    """
    if not path or not os.access(path, os.X_OK) or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as handle:
            return handle.read(len(_ELF)) == _ELF
    except OSError:
        return False


def bundled() -> Optional[str]:
    """The binary inside the installed package, if the wheel carries one."""
    path = Path(__file__).resolve().parent / "bin" / NAME
    return str(path) if _is_binary(str(path)) else None


def on_path() -> Optional[str]:
    """The first binary named eessi-arch-guard on PATH."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, NAME)
        if _is_binary(candidate):
            return candidate
    return None


def locate() -> Optional[str]:
    """The bundled binary, else one on PATH, else None."""
    return bundled() or on_path()


__all__ = ["NAME", "bundled", "locate", "on_path"]
