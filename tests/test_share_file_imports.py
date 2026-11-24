"""Share scripts remain importable by absolute file path from any directory."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ShareFileImportTests(unittest.TestCase):
    def test_scripts_load_by_file_path_outside_the_repository(self):
        with tempfile.TemporaryDirectory(prefix="eag-share-import-") as tmp:
            for name in ("cvmfsrev.py", "sitepath.py"):
                with self.subTest(script=name):
                    path = ROOT / "share" / name
                    code = (
                        "import importlib.util; "
                        f"spec = importlib.util.spec_from_file_location('checked', {str(path)!r}); "
                        "module = importlib.util.module_from_spec(spec); "
                        "spec.loader.exec_module(module)"
                    )
                    proc = subprocess.run(
                        [sys.executable, "-I", "-c", code],
                        cwd=tmp,
                        capture_output=True,
                        text=True,
                        timeout=2.0,
                        check=False,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
