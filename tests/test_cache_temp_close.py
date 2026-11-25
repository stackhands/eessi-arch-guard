"""A failed close of the cache temporary file, injected at ``os.close``.

``_write_cache`` writes the decision to a temporary file and renames it
over the cache name. A close that fails, such as a lost write on the
Network File System (NFS), has already freed the descriptor, so the
guard must not close it again: a second close raises EBADF, or closes a
descriptor that another thread has opened under the same number. The
failed close takes the ``OSError`` path: the temporary file is removed,
the cache name is left as it was, and ``_write_cache`` returns.
"""

from __future__ import annotations

import errno
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import eessi_arch_guard as guard  # noqa: E402


class CacheTempCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="eessi-arch-guard-cache-")
        self.addCleanup(shutil.rmtree, self.directory)
        env = {
            "TMPDIR": self.directory,
            "SLURM_JOB_ID": "91",
            "SLURM_STEP_ID": "batch",
        }
        self.path = guard.cache_file_path(env)

    def _write_with_failing_temp_close(self, outcome):
        original_open = os.open
        original_close = os.close
        temp_fds = []
        closed = []

        def tracking_open(path, flags, *args, **kwargs):
            fd = original_open(path, flags, *args, **kwargs)
            if str(path).endswith(".tmp"):
                temp_fds.append(fd)
            return fd

        def failing_close(fd):
            closed.append(fd)
            if fd in temp_fds and closed.count(fd) == 1:
                original_close(fd)
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            return original_close(fd)

        os.open = tracking_open
        os.close = failing_close
        try:
            guard._write_cache(self.path, outcome)
        finally:
            os.open = original_open
            os.close = original_close
        return temp_fds, closed

    def test_failed_temp_close_is_not_closed_again(self) -> None:
        guard._write_cache(self.path, guard.Outcome({"PATH": "/old"}, None, "reloaded", None))
        with open(self.path, "rb") as handle:
            before = handle.read()

        temp_fds, closed = self._write_with_failing_temp_close(
            guard.Outcome({"PATH": "/new"}, "moved", "reloaded", None)
        )

        self.assertEqual(len(temp_fds), 1, temp_fds)
        self.assertEqual(closed.count(temp_fds[0]), 1, closed)
        leftover = [name for name in os.listdir(self.directory) if name.endswith(".tmp")]
        self.assertEqual(leftover, [])
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_failed_temp_close_leaves_a_missing_cache_missing(self) -> None:
        temp_fds, closed = self._write_with_failing_temp_close(
            guard.Outcome({"PATH": "/new"}, "moved", "reloaded", None)
        )

        self.assertEqual(len(temp_fds), 1, temp_fds)
        self.assertEqual(closed.count(temp_fds[0]), 1, closed)
        self.assertEqual(os.listdir(self.directory), [])


if __name__ == "__main__":
    unittest.main()
