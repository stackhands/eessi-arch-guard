"""The ptrace tracers of the suite must build without a warning.

``test_write_close`` builds ``tests/fs_fail.c`` and
``test_cache_lock_close`` builds ``tests/close_fail.c``. Both build
without ``-Werror`` and skip their cases when the build fails, so a warning
there only shows as compiler output that the test then has to decode. Each
optimization level checks other things: gcc 11 warns about a possible
format truncation at every level, gcc 13 only at some of them. The cases
build both tracers with ``-Wall -Wextra -Werror`` at each level and fail on
any warning. They skip without gcc or off x86_64, as the tracer build does.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SOURCES = (TESTS / "fs_fail.c", TESTS / "close_fail.c")
LEVELS = ("-O0", "-O1", "-O2", "-O3", "-Os")
WARNINGS = ("-Wall", "-Wextra", "-Werror")


class TracerBuildTests(unittest.TestCase):
    def test_tracers_build_with_warnings_as_errors(self) -> None:
        if platform.machine() != "x86_64":
            self.skipTest("tests/fs_fail.c traces x86_64 system calls only")
        if shutil.which("gcc") is None:
            self.skipTest("gcc is not on PATH, so the tracers cannot be built")
        with tempfile.TemporaryDirectory(prefix="eag-tracer-build-") as directory:
            for source in SOURCES:
                for level in LEVELS:
                    with self.subTest(source=source.name, level=level):
                        out = Path(directory) / (source.stem + level)
                        built = subprocess.run(
                            ["gcc", level, *WARNINGS, "-o", str(out), str(source)],
                            capture_output=True,
                            encoding="utf-8",
                            errors="replace",
                            check=False,
                        )
                        self.assertEqual(built.returncode, 0, built.stderr or built.stdout)
                        self.assertEqual(built.stderr, "")


if __name__ == "__main__":
    unittest.main()
