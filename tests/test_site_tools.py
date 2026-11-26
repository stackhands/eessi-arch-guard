"""--check-mount and --check-revision, the two site tools of the binary."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import harness

SITE = "/opt/site/modules/all"
EESSI = "/cvmfs/software.eessi.io/versions/2026.06/software/linux/x86_64/amd/zen4/modules/all"
REPO = "/cvmfs/software.eessi.io"
MOUNT = (
    "36 35 0:36 / / rw,relatime - ext4 /dev/sda1 rw\n"
    "47 36 0:47 / /cvmfs/software.eessi.io rw,nosuid,nodev - fuse.cvmfs software.eessi.io rw\n"
)
PLAIN = "36 35 0:36 / / rw - ext4 /dev/sda1 rw\n"
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C", "LC_ALL": "C"}


def run(*args: str, env=None, timeout: float = 5.0):
    return harness.run_with_timeout(
        [*harness.GUARD_ARGV, *args], env=ENV if env is None else env, timeout=timeout
    )


class MountTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="eag-mount-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def mountinfo(self, text: str) -> str:
        path = self.root / "mountinfo"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def check(self, mount: str, modulepath: str, *extra: str):
        return run(
            "--check-mount",
            "--site",
            SITE,
            "--eessi",
            EESSI,
            "--modulepath",
            modulepath,
            "--mountinfo",
            self.mountinfo(mount),
            *extra,
        )

    def test_both_checks_pass_only_together(self) -> None:
        proc = self.check(MOUNT, f"{SITE}:{EESSI}")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            proc.stdout,
            f"ok   {REPO} is on a cvmfs mount (fuse.cvmfs)\n"
            f"ok   site modules {SITE} precede {EESSI}\n",
        )
        proc = self.check(MOUNT, f"{EESSI}:{SITE}")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(f"FAIL site modules {SITE} are behind {EESSI}\n", proc.stdout)
        proc = self.check(PLAIN, f"{SITE}:{EESSI}")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(f"FAIL {REPO} is not on a cvmfs mount (ext4)\n", proc.stdout)

    def test_a_repository_with_no_mount_is_named(self) -> None:
        proc = self.check("", f"{SITE}:{EESSI}")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(f"FAIL {REPO} is not on a cvmfs mount (no mount)\n", proc.stdout)

    def test_each_module_path_problem_has_its_own_line(self) -> None:
        cases = {
            f"{EESSI}": f"FAIL site modules {SITE} are not on MODULEPATH\n",
            f"{SITE}": f"FAIL EESSI modules {EESSI} are not on MODULEPATH\n",
            "/other/modules/all": f"FAIL neither {SITE} nor {EESSI} is on MODULEPATH\n",
            f"/opt/site-extra/modules/all:{EESSI}": (
                f"FAIL site modules {SITE} are not on MODULEPATH\n"
            ),
        }
        for modulepath, line in cases.items():
            with self.subTest(modulepath=modulepath):
                proc = self.check(MOUNT, modulepath)
                self.assertEqual(proc.returncode, 1)
                self.assertIn(line, proc.stdout)
                self.assertNotIn("same path", proc.stdout)

    def test_one_path_for_both_is_the_same_path(self) -> None:
        proc = run(
            "--check-mount",
            "--site",
            SITE,
            "--eessi",
            SITE,
            "--modulepath",
            SITE,
            "--mountinfo",
            self.mountinfo(MOUNT),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn(f"FAIL site modules {SITE} and the EESSI modules are the same path\n", proc.stdout)

    def test_repeated_and_trailing_slashes_are_normalized(self) -> None:
        proc = run(
            "--check-mount",
            "--repo",
            "/cvmfs//software.eessi.io",
            "--site",
            SITE,
            "--eessi",
            EESSI,
            "--modulepath",
            f"/opt/site//modules/all/:{EESSI}/",
            "--mountinfo",
            self.mountinfo(MOUNT),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_install_prefixes_from_the_environment_get_modules_all_once(self) -> None:
        env = {
            **ENV,
            "EESSI_SITE_SOFTWARE_PATH": SITE + "/",
            "EESSI_SOFTWARE_PATH": EESSI + "/",
            "MODULEPATH": f"{SITE}:{EESSI}",
        }
        proc = run("--check-mount", "--mountinfo", self.mountinfo(MOUNT), env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"ok   site modules {SITE} precede {EESSI}", proc.stdout)

    def test_install_prefixes_without_modules_all_get_it_added(self) -> None:
        env = {
            **ENV,
            "EESSI_SITE_SOFTWARE_PATH": "/opt/site",
            "EESSI_SOFTWARE_PATH": EESSI[: -len("/modules/all")],
            "MODULEPATH": f"{SITE}:{EESSI}",
        }
        proc = run("--check-mount", "--mountinfo", self.mountinfo(MOUNT), env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_the_repository_comes_from_the_environment(self) -> None:
        env = {**ENV, "EESSI_CVMFS_REPO": "/cvmfs/other.example"}
        proc = run(
            "--check-mount",
            "--site",
            SITE,
            "--eessi",
            EESSI,
            "--modulepath",
            f"{SITE}:{EESSI}",
            "--mountinfo",
            self.mountinfo(MOUNT),
            env=env,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAIL /cvmfs/other.example is not on a cvmfs mount (ext4)", proc.stdout)

    def test_the_longest_and_last_mount_wins(self) -> None:
        mount = (
            "36 35 0:36 / / rw - ext4 /dev/sda1 rw\n"
            "47 36 0:47 / /cvmfs rw - ext4 /dev/sdb1 rw\n"
            "48 47 0:48 / /cvmfs/software.eessi.io rw - fuse.cvmfs software.eessi.io rw\n"
            "49 48 0:49 / /cvmfs/software.eessi.io rw - overlay none rw\n"
        )
        proc = self.check(mount, f"{SITE}:{EESSI}")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(f"FAIL {REPO} is not on a cvmfs mount (overlay)", proc.stdout)

    def test_an_escaped_mount_point_is_unescaped(self) -> None:
        mount = (
            "36 35 0:36 / / rw - ext4 /dev/sda1 rw\n"
            "48 36 0:48 / /cvmfs/software\\040eessi rw - fuse.cvmfs x rw\n"
        )
        proc = run(
            "--check-mount",
            "--repo",
            "/cvmfs/software eessi/sub",
            "--site",
            SITE,
            "--eessi",
            EESSI,
            "--modulepath",
            f"{SITE}:{EESSI}",
            "--mountinfo",
            self.mountinfo(mount),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("is on a cvmfs mount (fuse.cvmfs)", proc.stdout)

    def test_the_paths_are_required(self) -> None:
        proc = run("--check-mount", "--mountinfo", self.mountinfo(MOUNT))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("needs --site and --eessi", proc.stderr)

    def test_require_input_rejects_placeholder_paths(self) -> None:
        for site, eessi in (
            ("/modules/all", "/modules/all"),
            ("/", EESSI),
            ("   ", EESSI),
        ):
            with self.subTest(site=site):
                env = {
                    **ENV,
                    "EESSI_SITE_SOFTWARE_PATH": site,
                    "EESSI_SOFTWARE_PATH": eessi,
                    "MODULEPATH": f"/modules/all:{EESSI}",
                }
                proc = run(
                    "--check-mount", "--require-input", "--mountinfo", self.mountinfo(MOUNT), env=env
                )
                self.assertEqual(proc.returncode, 2, (proc.stdout, proc.stderr))
                if site.strip():
                    self.assertIn("--require-input", proc.stderr)

    def test_require_input_rejects_explicit_placeholder_arguments(self) -> None:
        proc = run(
            "--check-mount",
            "--require-input",
            "--site",
            "/modules/all",
            "--eessi",
            "/modules/all",
            "--mountinfo",
            "/proc/self/mountinfo",
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--require-input", proc.stderr)

    def test_a_placeholder_path_without_the_flag_still_runs(self) -> None:
        proc = run(
            "--check-mount",
            "--site",
            "/modules/all",
            "--eessi",
            "/modules/all",
            "--modulepath",
            "/modules/all",
            "--mountinfo",
            self.mountinfo(PLAIN),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAIL", proc.stdout)

    def test_a_mountinfo_that_is_not_a_regular_file_is_refused(self) -> None:
        fifo = self.root / "mountinfo.fifo"
        os.mkfifo(fifo, 0o600)
        directory = self.root / "mountinfo.dir"
        directory.mkdir()
        for path in (fifo, directory):
            with self.subTest(path=path.name):
                proc = run(
                    "--check-mount",
                    "--site",
                    SITE,
                    "--eessi",
                    EESSI,
                    "--modulepath",
                    f"{SITE}:{EESSI}",
                    "--mountinfo",
                    str(path),
                    timeout=1.5,
                )
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(
                    proc.stderr, f"eessi-arch-guard: cannot read {path}: not a regular file\n"
                )

    def test_a_missing_mountinfo_is_named(self) -> None:
        path = self.root / "absent"
        proc = run(
            "--check-mount", "--site", SITE, "--eessi", EESSI, "--mountinfo", str(path)
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            proc.stderr, f"eessi-arch-guard: cannot read {path}: No such file or directory\n"
        )


class RevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="eag-revision-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def stat(self, data: bytes, *extra: str):
        path = self.root / "stat"
        path.write_bytes(data)
        return run("--check-revision", "--stat", str(path), *extra, timeout=1.5)

    def test_without_a_minimum_it_reports_and_does_not_fail(self) -> None:
        proc = self.stat(b"Revision: 42\nExpires in: 4 minutes\n")
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "revision 42\n", ""))

    def test_the_minimum_is_compared(self) -> None:
        data = b"Revision: 42\n"
        for minimum, code in (("40", 0), ("42", 0), ("50", 1)):
            with self.subTest(minimum=minimum):
                proc = self.stat(data, "--min", minimum)
                self.assertEqual(proc.returncode, code, proc.stderr)
        proc = self.stat(data, "--min", "50")
        self.assertEqual(proc.stderr, "behind revision 42 is older than 50\n")
        self.assertEqual(proc.stdout, "")

    def test_other_labels_are_read(self) -> None:
        self.assertEqual(self.stat(b"Catalog revision: 7\n").stdout, "revision 7\n")
        self.assertEqual(self.stat(b"Repository revision: 9\n").stdout, "revision 9\n")

    def test_no_revision_is_an_error(self) -> None:
        proc = self.stat(b"Connected via proxy DIRECT\n", "--min", "50")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("no revision in the stat output", proc.stderr)

    def test_stat_rejects_non_regular_and_dangling_stat(self) -> None:
        fifo = self.root / "stat.fifo"
        os.mkfifo(fifo, 0o600)
        directory = self.root / "stat.dir"
        directory.mkdir()
        dangle = self.root / "stat.dangle"
        dangle.symlink_to(self.root / "absent.txt")
        for path in (fifo, directory, dangle):
            with self.subTest(path=path.name):
                proc = run("--check-revision", "--stat", str(path), timeout=1.5)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn(f"eessi-arch-guard: cannot read {path}: ", proc.stderr)

    def test_stat_rejects_oversized_file(self) -> None:
        proc = self.stat(b"revision: 42\n" + b"x" * ((1 << 20) + 64))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("file too large", proc.stderr)

    def test_stat_with_non_utf8_bytes_is_read(self) -> None:
        proc = self.stat(b"Comment: \xff\xfe\nRevision: 42\n")
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "revision 42"))

    def test_stat_strips_a_leading_utf8_bom(self) -> None:
        proc = self.stat(b"\xef\xbb\xbfRevision: 42\n")
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "revision 42"))

    def test_arguments_are_checked(self) -> None:
        proc = run("--check-revision")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--stat", proc.stderr)
        proc = self.stat(b"Revision: 42\n", "--min=-1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--min must be zero or more", proc.stderr)
        proc = self.stat(b"Revision: 42\n", "--min", "many")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--min needs an integer", proc.stderr)


if __name__ == "__main__":
    unittest.main()
