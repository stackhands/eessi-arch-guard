"""The CVMFS client revision, compared only when a site asks."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402


def load():
    path = ROOT / "share" / "cvmfsrev.py"
    spec = importlib.util.spec_from_file_location("cvmfsrev", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RevisionTests(unittest.TestCase):
    def test_stat_output_names_one_revision(self):
        rev = load()
        self.assertEqual(rev.revision_of("Revision: 42\nExpires in: 4 minutes\n"), 42)
        self.assertEqual(rev.revision_of("Catalog revision: 7\n"), 7)
        self.assertEqual(rev.revision_of("Repository revision: 9\n"), 9)
        self.assertIsNone(rev.revision_of("Connected via proxy DIRECT\n"))

    def test_without_a_minimum_it_reports_and_does_not_fail(self):
        rev = load()
        code, text = rev.gate(42, None)
        self.assertEqual(code, 0)
        self.assertEqual(text, "revision 42\n")
        code, text = rev.gate(42, 40)
        self.assertEqual(code, 0, text)
        code, text = rev.gate(42, 42)
        self.assertEqual(code, 0, text)
        code, text = rev.gate(42, 50)
        self.assertEqual(code, 1)
        self.assertIn("behind", text)
        code, text = rev.gate(None, 50)
        self.assertEqual(code, 2)
        self.assertIn("no revision", text)

    def test_stat_rejects_non_regular_and_dangling_stat(self):
        script = str(ROOT / "share" / "cvmfsrev.py")
        with tempfile.TemporaryDirectory(prefix="eessi-cvmfsrev-") as tmp:
            root = Path(tmp)
            fifo = root / "stat.fifo"
            os.mkfifo(fifo, 0o600)
            sdir = root / "stat.dir"
            sdir.mkdir()
            dangle = root / "stat.dangle"
            dangle.symlink_to(root / "absent.txt")
            for path in (fifo, sdir, dangle):
                with self.subTest(path=path.name):
                    proc = harness.run_with_timeout(
                        [sys.executable, script, "--stat", str(path)],
                        timeout=1.5,
                    )
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(proc.stdout, "")
                    self.assertIn("cvmfsrev.py: cannot read ", proc.stderr)

    def test_stat_rejects_oversized_file(self):
        script = str(ROOT / "share" / "cvmfsrev.py")
        with tempfile.TemporaryDirectory(prefix="eessi-cvmfsrev-") as tmp:
            big = Path(tmp) / "stat.big"
            big.write_bytes(b"revision: 42\n" + b"x" * ((1 << 20) + 64))
            proc = harness.run_with_timeout(
                [sys.executable, script, "--stat", str(big)],
                timeout=1.5,
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn("file too large", proc.stderr)

    def test_stat_decodes_non_utf8_bytes_with_replacement(self):
        script = str(ROOT / "share" / "cvmfsrev.py")
        with tempfile.TemporaryDirectory(prefix="eessi-cvmfsrev-") as tmp:
            stat_file = Path(tmp) / "stat.bin"
            stat_file.write_bytes(b"Comment: \xff\xfe\nRevision: 42\n")
            proc = harness.run_with_timeout(
                [sys.executable, script, "--stat", str(stat_file)],
                timeout=1.5,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "revision 42")

    def test_stat_strips_leading_utf8_bom(self):
        script = str(ROOT / "share" / "cvmfsrev.py")
        with tempfile.TemporaryDirectory(prefix="eessi-cvmfsrev-") as tmp:
            stat_file = Path(tmp) / "stat.bom"
            stat_file.write_bytes(b"\xef\xbb\xbfRevision: 42\n")
            proc = harness.run_with_timeout(
                [sys.executable, script, "--stat", str(stat_file)],
                timeout=1.5,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "revision 42")


if __name__ == "__main__":
    unittest.main()
