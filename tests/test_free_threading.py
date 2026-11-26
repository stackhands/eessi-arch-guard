"""The launcher works on a free-threaded CPython.

Importing it must not turn the GIL back on: CPython re-enables the GIL when it
imports an extension module that does not declare free-threading support, and
the package carries ``_launch``, so the probe imports it and reads the GIL
afterwards. ``locate`` keeps no shared state, so calls from many threads at
once give the answer one thread gets; ``_launch.execv`` on a missing path
fails with ENOENT in every thread and leaves the process in place.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path

import harness

PROBE = Path(__file__).resolve().parent / "free_threading_probe.py"


class FreeThreadingTests(unittest.TestCase):
    def test_the_gil_stays_as_built_and_threads_agree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eag-ft-") as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            binary = bindir / "eessi-arch-guard"
            shutil.copy2(harness.PROGRAM, binary)
            env = {k: v for k, v in os.environ.items() if k != "PYTHON_GIL"}
            env["PATH"] = str(bindir)
            env["PYTHONPATH"] = str(harness.ROOT / "src")
            done = harness.run_with_timeout(
                [sys.executable, str(PROBE)], env=env, timeout=60.0, cwd=tmp
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            report = json.loads(done.stdout)
            version = harness.run_with_timeout([str(binary), "--version"]).stdout
        free = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
        self.assertEqual(report["free_threaded_build"], free)
        if free:
            self.assertIs(report["gil"], False)
        elif hasattr(sys, "_is_gil_enabled"):
            self.assertIs(report["gil"], True)
        else:
            self.assertIsNone(report["gil"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["exec_errors"], [errno.ENOENT])
        self.assertEqual(report["answers"], [[str(binary), 0, version]])


if __name__ == "__main__":
    unittest.main()
