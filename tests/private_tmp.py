"""Give this test process a temporary directory no other process uses.

Importing the module makes a new directory under the system one and points
``TMPDIR`` and ``tempfile.tempdir`` at it, so the files and directories a test
makes, and those of a guard it starts with the inherited environment, land
there. The directory is removed when the process exits. Two test runs at the
same time then share nothing under ``/tmp``.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

ROOT = tempfile.mkdtemp(prefix="eag-tests-")
os.environ["TMPDIR"] = ROOT
tempfile.tempdir = ROOT
atexit.register(shutil.rmtree, ROOT, True)
