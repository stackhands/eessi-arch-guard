"""A failed close of the cache lock, injected at the close(2) call.

The guard releases the cache lock by closing its descriptor, and Python
closes it in a ``finally`` block, so a failed close raises OSError. After a
decision, cached or new, both builds print the skip line of that OSError.
After the deadline or SIGTERM the Zig build keeps the timeout or the
termination, where Python's ``finally`` block reports the close instead;
COMPAT.md lists the difference. An interrupted close is a failed one too:
``os.close`` does not retry EINTR, as PEP 475 exempts it, and raises
InterruptedError.

``close_fail.c`` traces the guard and fails the first close of the lock
with EIO, or EINTR, after the close has run, so the lock is free
afterwards, as after a real failed close. The tests run the build
``harness`` picks; they skip without a C compiler or where ptrace is
refused.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import eessi_arch_guard as guard  # noqa: E402
import harness  # noqa: E402
from slurm_model import apply_task_prolog  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"
EIO = "5"
EINTR = "4"
SKIP_LINE = "print eessi-arch-guard: skipped, OSError: [Errno 5] Input/output error\n"
EINTR_SKIP_LINE = (
    "print eessi-arch-guard: skipped, InterruptedError: [Errno 4] Interrupted system call\n"
)
SOURCE = Path(__file__).resolve().parent / "close_fail.c"


class CacheLockCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = shutil.which("cc")
        if compiler is None:
            raise unittest.SkipTest("no C compiler to build close_fail")
        cls.build_dir = tempfile.mkdtemp(prefix="eag-close-fail-")
        cls.tracer = os.path.join(cls.build_dir, "close_fail")
        subprocess.run([compiler, "-O2", "-o", cls.tracer, str(SOURCE)], check=True)
        probe = subprocess.run(
            [cls.tracer, EIO, "/nonexistent", "--", "true"], capture_output=True, check=False
        )
        if probe.returncode != 0:
            shutil.rmtree(cls.build_dir, ignore_errors=True)
            raise unittest.SkipTest("ptrace is refused here: %r" % probe.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.build_dir, ignore_errors=True)

    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.site.write_cpuinfo("zen2")
        self.env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        self.env["TMPDIR"] = self.site.root
        self.env["SLURM_JOB_ID"] = "7373"
        self.cache = guard.cache_file_path(self.env)
        self.lock = self.cache + ".lock"

    def _argv(self, args=(), errno=EIO):
        return [self.tracer, errno, self.lock, "--", *harness.GUARD_ARGV, *args]

    def _run(self, args=(), errno=EIO):
        return subprocess.run(
            self._argv(args, errno),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
            check=False,
        )

    def _assert_lock_free(self) -> None:
        fd = os.open(self.lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def _hold_lock(self) -> int:
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        return fd

    def _failed_token(self, proc) -> str:
        self.assertEqual(proc.returncode, 0, proc.stderr)
        changes, _printed = apply_task_prolog(proc.stdout)
        return changes["EESSI_ARCH_GUARD_FAILED"]

    def test_failed_close_after_a_decision_is_the_skip(self) -> None:
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, SKIP_LINE)
        self.assertEqual(proc.stderr, "")
        # The decision was written before the close, as in Python.
        self.assertTrue(os.path.isfile(self.cache))
        self._assert_lock_free()

    def test_interrupted_close_after_a_decision_is_the_interrupted_error_skip(self) -> None:
        proc = self._run(errno=EINTR)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, EINTR_SKIP_LINE)
        self.assertEqual(proc.stderr, "")
        self.assertTrue(os.path.isfile(self.cache))
        self._assert_lock_free()

    def test_failed_close_after_a_cache_hit_is_the_skip_and_strict(self) -> None:
        guard._write_cache(
            self.cache,
            guard.Outcome({"SENTINEL": "cached-decision"}, "cached", "reloaded"),
        )
        before = Path(self.cache).read_bytes()
        proc = self._run(["--strict"])
        self.assertEqual(proc.returncode, guard.STRICT_FAILURE_EXIT, proc.stderr)
        self.assertEqual(proc.stdout, SKIP_LINE)
        self.assertEqual(Path(self.cache).read_bytes(), before)
        self._assert_lock_free()

    def test_closes_that_succeed_print_the_decision(self) -> None:
        proc = subprocess.run(
            [self.tracer, EIO, "/nonexistent", "--", *harness.GUARD_ARGV],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        changes, _printed = apply_task_prolog(proc.stdout)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2)

    def test_failed_close_after_the_deadline(self) -> None:
        self._hold_lock()
        proc = self._run(["--timeout", "0.5"])
        if harness.TOOL_BIN:
            self.assertTrue(self._failed_token(proc).endswith("->timeout"), proc.stdout)
        else:
            self.assertEqual(proc.stdout, SKIP_LINE)
        self.assertFalse(os.path.isfile(self.cache))

    def test_failed_close_after_sigterm(self) -> None:
        self._hold_lock()
        tracer = subprocess.Popen(
            self._argv(["--timeout", "30"]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        try:
            self._wait_for_lock_open(tracer)
            tracer.send_signal(signal.SIGTERM)
            out, err = tracer.communicate(timeout=30)
        finally:
            if tracer.poll() is None:
                tracer.kill()
                tracer.communicate(timeout=10)
        proc = subprocess.CompletedProcess(tracer.args, tracer.returncode, out, err)
        if harness.TOOL_BIN:
            self.assertTrue(self._failed_token(proc).endswith("->terminated"), proc.stdout)
        else:
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, SKIP_LINE)
        self.assertFalse(os.path.isfile(self.cache))

    def _wait_for_lock_open(self, tracer) -> None:
        """Wait until the traced guard holds the lock file open.

        It installs its SIGTERM handler before it opens the lock, so a
        SIGTERM from then on reaches the handler.
        """
        children = "/proc/%d/task/%d/children" % (tracer.pid, tracer.pid)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if tracer.poll() is not None:
                self.fail("the guard ended before it opened the lock: %r" % (tracer.stderr.read(),))
            try:
                pids = Path(children).read_text(encoding="ascii").split()
            except FileNotFoundError:
                self.skipTest("no %s to find the traced guard" % children)
            for pid in pids:
                fd_dir = "/proc/%s/fd" % pid
                try:
                    links = [os.readlink(os.path.join(fd_dir, fd)) for fd in os.listdir(fd_dir)]
                except OSError:
                    continue
                if self.lock in links:
                    return
            time.sleep(0.02)
        self.fail("the guard did not open %s" % self.lock)


if __name__ == "__main__":
    unittest.main()
