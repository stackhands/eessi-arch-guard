"""The Python launcher: it finds the binary and replaces itself with it."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import harness

ROOT = harness.ROOT
SRC = ROOT / "src"
SHARE = ROOT / "share"


def run_python(args, path_dirs, pythonpath, cwd=None):
    env = {
        "PATH": os.pathsep.join(path_dirs),
        "PYTHONPATH": str(pythonpath),
        "LANG": "C",
        "LC_ALL": "C",
        "HOME": "/tmp",
    }
    return harness.run_with_timeout([sys.executable, *args], env=env, timeout=10.0, cwd=cwd)


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="eag-package-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        shutil.copy2(harness.PROGRAM, self.bindir / "eessi-arch-guard")
        self.version = harness.run_with_timeout([*harness.GUARD_ARGV, "--version"]).stdout

    def test_the_binary_on_path_is_run_with_the_arguments(self) -> None:
        proc = run_python(["-m", "eessi_arch_guard", "--version"], [str(self.bindir)], SRC)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, self.version, ""))

    def test_the_exit_status_and_streams_are_the_binarys(self) -> None:
        proc = run_python(["-m", "eessi_arch_guard", "--check-revision"], [str(self.bindir)], SRC)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--stat", proc.stderr)

    def test_the_bundled_binary_comes_before_path(self) -> None:
        package = self.root / "site" / "eessi_arch_guard"
        shutil.copytree(
            SRC / "eessi_arch_guard", package, ignore=shutil.ignore_patterns("__pycache__")
        )
        (package / "bin").mkdir(exist_ok=True)
        shutil.copy2(harness.PROGRAM, package / "bin" / "eessi-arch-guard")
        decoy = self.root / "decoy"
        decoy.mkdir()
        (decoy / "eessi-arch-guard").write_bytes(b"\x7fELFnot a program")
        os.chmod(decoy / "eessi-arch-guard", 0o755)
        proc = run_python(["-m", "eessi_arch_guard", "--version"], [str(decoy)], self.root / "site")
        self.assertEqual((proc.returncode, proc.stdout), (0, self.version), proc.stderr)

    def test_no_binary_is_a_clear_error(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        proc = run_python(["-m", "eessi_arch_guard", "--version"], [str(empty)], SRC)
        self.assertEqual(proc.returncode, 127)
        self.assertEqual(proc.stdout, "")
        self.assertIn("eessi-arch-guard: no binary found", proc.stderr)

    def test_a_script_of_the_same_name_is_not_the_binary(self) -> None:
        shim = self.root / "shim"
        shim.mkdir()
        (shim / "eessi-arch-guard").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(shim / "eessi-arch-guard", 0o755)
        proc = run_python(["-m", "eessi_arch_guard", "--version"], [str(shim)], SRC)
        self.assertEqual(proc.returncode, 127, proc.stderr)

    def test_locate_reports_the_path_binary(self) -> None:
        code = "import eessi_arch_guard as g; print(g.locate()); print(g.bundled())"
        proc = run_python(["-c", code], [str(self.bindir)], SRC)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        found, bundled = proc.stdout.split("\n")[:2]
        self.assertEqual(found, str(self.bindir / "eessi-arch-guard"))
        self.assertEqual(bundled, "None")

    def test_the_package_version_is_the_binarys(self) -> None:
        proc = run_python(["-c", "import eessi_arch_guard as g; print(g.__version__)"], [], SRC)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), self.version.split()[0])


class SiteScriptTests(unittest.TestCase):
    """share/sitepath.py and share/cvmfsrev.py call the binary."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="eag-share-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        shutil.copy2(harness.PROGRAM, self.bindir / "eessi-arch-guard")

    def test_sitepath_gives_the_output_of_check_mount(self) -> None:
        mountinfo = self.root / "mountinfo"
        mountinfo.write_text(
            "36 35 0:36 / / rw - ext4 /dev/sda1 rw\n"
            "47 36 0:47 / /cvmfs/software.eessi.io rw - fuse.cvmfs software.eessi.io rw\n",
            encoding="utf-8",
        )
        args = [
            "--site", "/opt/site/modules/all",
            "--eessi", "/cvmfs/software.eessi.io/m/modules/all",
            "--modulepath", "/opt/site/modules/all:/cvmfs/software.eessi.io/m/modules/all",
            "--mountinfo", str(mountinfo),
        ]  # fmt: skip
        direct = harness.run_with_timeout([*harness.GUARD_ARGV, "--check-mount", *args])
        script = run_python([str(SHARE / "sitepath.py"), *args], [str(self.bindir)], SRC)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertEqual(
            (script.returncode, script.stdout, script.stderr),
            (direct.returncode, direct.stdout, direct.stderr),
        )

    def test_cvmfsrev_gives_the_output_of_check_revision(self) -> None:
        stat = self.root / "stat"
        stat.write_text("Revision: 42\n", encoding="utf-8")
        for extra, code in (([], 0), (["--min", "50"], 1)):
            with self.subTest(extra=extra):
                script = run_python(
                    [str(SHARE / "cvmfsrev.py"), "--stat", str(stat), *extra],
                    [str(self.bindir)],
                    SRC,
                )
                direct = harness.run_with_timeout(
                    [*harness.GUARD_ARGV, "--check-revision", "--stat", str(stat), *extra]
                )
                self.assertEqual(script.returncode, code, script.stderr)
                self.assertEqual(
                    (script.returncode, script.stdout, script.stderr),
                    (direct.returncode, direct.stdout, direct.stderr),
                )

    def test_the_scripts_run_without_the_package(self) -> None:
        stat = self.root / "stat"
        stat.write_text("Revision: 42\n", encoding="utf-8")
        proc = run_python(
            [str(SHARE / "cvmfsrev.py"), "--stat", str(stat)], [str(self.bindir)], self.root
        )
        self.assertEqual((proc.returncode, proc.stdout), (0, "revision 42\n"), proc.stderr)

    def test_a_missing_binary_is_named(self) -> None:
        proc = run_python([str(SHARE / "sitepath.py")], [], self.root)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("sitepath.py: eessi-arch-guard is not installed", proc.stderr)


if __name__ == "__main__":
    unittest.main()
