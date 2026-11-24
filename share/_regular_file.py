"""Non-blocking reads for command inputs that must be regular files."""

from __future__ import annotations

import os
import stat
from typing import Optional


def read_regular_bytes(path: str, max_bytes: Optional[int] = None) -> bytes:
    """Read a regular file, refusing FIFOs and directories before reading."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(0, "not a regular file")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise OSError(0, "file too large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)
