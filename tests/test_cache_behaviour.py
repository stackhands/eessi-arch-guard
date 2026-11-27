"""The decision cache of a Slurm job step, from the outside.

A run with ``SLURM_JOB_ID`` set keeps its decision in a private file under
``TMPDIR``. The path comes from ``--dry-run --json``. The fake Lmod logs each
reload, so a decision made again shows in that log.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
from slurm_model import apply_task_prolog  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"
FS_FAIL = Path(__file__).resolve().parent / "fs_fail.c"


class CacheCase(unittest.TestCase):
    def setUp(self):
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.site.write_cpuinfo("zen2")
        self.env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        self.env["TMPDIR"] = self.site.root
        self.env["SLURM_JOB_ID"] = "8181"
        self.env["SLURM_STEP_ID"] = "batch"

    def cache_path(self, shell=False, env=None):
        args = ["--dry-run", "--json"] + (["--shell"] if shell else [])
        proc = harness.run_guard(env or self.env, args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = json.loads(proc.stdout)["cache"]["path"]
        self.assertIsNotNone(path)
        return path

    def reloads(self):
        return sum(1 for line in harness.log_lines(self.site.log) if line == "purge")

    def run_guard(self, args=(), env=None):
        proc = harness.run_guard(env or self.env, args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc


class CacheTests(CacheCase):
    def test_the_decision_is_made_once_and_then_read(self):
        first = self.run_guard()
        second = self.run_guard()
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(self.reloads(), 1)
        self.assertTrue(os.path.exists(self.cache_path()))

    def test_cache_is_kept_per_output_mode(self):
        # A newline in a value reloads for --shell and fails in a prolog, so one
        # mode's cached result must not answer the other.
        self.env["EESSI_TEST_SENTINEL"] = "a\nb"
        shell_path = self.cache_path(shell=True)
        prolog_path = self.cache_path(shell=False)
        self.assertNotEqual(shell_path, prolog_path)
        first = self.run_guard(["--shell"])
        self.assertEqual(self.reloads(), 1)
        second = self.run_guard()
        self.assertEqual(self.reloads(), 2)
        third = self.run_guard(["--shell"])
        self.assertEqual(self.reloads(), 2)
        self.assertEqual(first.stdout, third.stdout)
        self.assertIn("ROUNDTRIP", first.stdout)
        changes, _printed = apply_task_prolog(second.stdout)
        self.assertEqual(set(changes), {"EESSI_ARCH_GUARD_FAILED"})
        self.assertTrue(os.path.exists(shell_path))
        self.assertTrue(os.path.exists(prolog_path))

    def test_cache_read_needs_an_owned_private_regular_file(self):
        self.run_guard()
        path = self.cache_path()
        self.assertEqual(self.reloads(), 1)
        self.run_guard()
        self.assertEqual(self.reloads(), 1)
        os.chmod(path, 0o640)
        self.run_guard()
        self.assertEqual(self.reloads(), 2)
        os.chmod(path, 0o600)
        twin = os.path.join(self.site.root, "twin")
        os.link(path, twin)
        self.run_guard()
        self.assertEqual(self.reloads(), 3)
        os.unlink(twin)
        self.run_guard()
        self.assertEqual(self.reloads(), 3)

    def test_cache_read_does_not_block_on_a_fifo(self):
        path = self.cache_path()
        os.mkfifo(path, 0o600)
        proc = subprocess.run(
            [*harness.GUARD_ARGV],
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("EESSI_SOFTWARE_SUBDIR=%s" % ZEN2, proc.stdout)
        self.assertEqual(self.reloads(), 1)

    def test_cached_changes_obey_the_never_touch_list(self):
        self.run_guard()
        path = self.cache_path()
        for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "SLURM_JOB_ID", "BASH_FUNC_x%%", "TMPDIR"):
            with self.subTest(key=key):
                before = self.reloads()
                body = {
                    "v": 1,
                    "kind": "reloaded",
                    "message": None,
                    "detail": None,
                    "changes": {key: "/evil.so"},
                }
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(body, handle)
                os.chmod(path, 0o600)
                proc = self.run_guard()
                self.assertNotIn("/evil.so", proc.stdout)
                self.assertEqual(self.reloads(), before + 1)

    def test_a_cached_decision_with_a_clean_change_is_used(self):
        self.run_guard()
        path = self.cache_path()
        body = {"v": 1, "kind": "reloaded", "message": None, "detail": None, "changes": {"X": "y"}}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(body, handle)
        os.chmod(path, 0o600)
        before = self.reloads()
        proc = self.run_guard()
        self.assertEqual(proc.stdout, "export X=y\n")
        self.assertEqual(self.reloads(), before)

    def test_untrusted_lock_bypasses_the_cache(self):
        path = self.cache_path()
        lock = path + ".lock"
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(lock, 0o666)
        proc = self.run_guard()
        self.assertIn("EESSI_SOFTWARE_SUBDIR=%s" % ZEN2, proc.stdout)
        self.assertEqual(self.reloads(), 1)
        self.assertFalse(os.path.exists(path), "an untrusted lock must not lead to a cache write")
        self.assertEqual(os.stat(lock).st_mode & 0o777, 0o666)

    def test_the_cache_directory_falls_back_to_tmp(self):
        env = dict(self.env)
        env["TMPDIR"] = os.path.join(self.site.root, "no-such-directory")
        path = self.cache_path(env=env)
        self.assertEqual(os.path.dirname(path), "/tmp")
        self.assertFalse(os.path.exists(env["TMPDIR"]))

    def test_the_name_ignores_the_guard_switches_and_follows_the_bind_list_and_gpu(self):
        left = dict(self.env, EESSI_ARCH_GUARD_MISSING="ignore", EESSI_ARCH_GUARD_VERBOSE="0")
        self.assertEqual(self.cache_path(env=left), self.cache_path(env=self.env))
        changed = dict(self.env, LOADEDMODULES="EESSI/2025.06:Other/1.0")
        self.assertNotEqual(self.cache_path(env=changed), self.cache_path(env=self.env))
        bound = dict(self.env, SLURM_CPU_BIND_LIST="cpu[0]")
        other = dict(self.env, SLURM_CPU_BIND_LIST="cpu[1]")
        self.assertNotEqual(self.cache_path(env=bound), self.cache_path(env=other))
        gpu0 = dict(self.env, CUDA_VISIBLE_DEVICES="0")
        gpu1 = dict(self.env, CUDA_VISIBLE_DEVICES="1")
        self.assertNotEqual(self.cache_path(env=gpu0), self.cache_path(env=gpu1))
        self.assertEqual(self.cache_path(env=gpu0), self.cache_path(env=dict(gpu0)))


class CacheTemporaryFileTests(CacheCase):
    """A failed open, write or close of the file the cache is written through."""

    @classmethod
    def setUpClass(cls):
        if platform.machine() != "x86_64":
            raise unittest.SkipTest("tests/fs_fail.c traces x86_64 system calls only")
        compiler = shutil.which("gcc") or shutil.which("cc")
        if compiler is None:
            raise unittest.SkipTest("no C compiler to build fs_fail")
        cls.build_dir = tempfile.mkdtemp(prefix="eag-fs-fail-")
        cls.addClassCleanup(shutil.rmtree, cls.build_dir, ignore_errors=True)
        cls.tracer = os.path.join(cls.build_dir, "fs_fail")
        subprocess.run([compiler, "-O2", "-o", cls.tracer, str(FS_FAIL)], check=True)
        probe = subprocess.run(
            [cls.tracer, "5", "close", "1", "/nonexistent", "--", "true"],
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            raise unittest.SkipTest("ptrace is refused here: %r" % probe.stderr)

    def fail(self, operation):
        """Run the guard with the first ``operation`` on a cache temporary file failing."""
        proc = subprocess.run(
            [self.tracer, "5", operation, "1", ".tmp", "--", *harness.GUARD_ARGV],
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        leftover = [n for n in os.listdir(self.site.root) if n.endswith(".tmp")]
        return proc, leftover

    def stale_cache(self):
        self.run_guard()
        path = self.cache_path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("stale")
        os.chmod(path, 0o640)
        return path

    def test_a_failed_open_or_write_leaves_the_cache_as_it_was(self):
        for operation in ("open", "write"):
            with self.subTest(operation=operation):
                path = self.stale_cache()
                proc, leftover = self.fail(operation)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("EESSI_SOFTWARE_SUBDIR=%s" % ZEN2, proc.stdout)
                self.assertEqual(leftover, [])
                with open(path, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "stale")

    def test_a_failed_close_still_prints_the_decision_and_leaves_no_temporary_file(self):
        path = self.stale_cache()
        proc, leftover = self.fail("close")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("EESSI_SOFTWARE_SUBDIR=%s" % ZEN2, proc.stdout)
        self.assertEqual(leftover, [])
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "stale")

    def test_a_failed_close_with_no_cache_leaves_no_temporary_file(self):
        proc, leftover = self.fail("close")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(leftover, [])


if __name__ == "__main__":
    unittest.main()
