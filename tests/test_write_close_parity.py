"""The Python and Zig guards must report a failed close of a file they write.

The restore helper closes the shell code it runs with ``os.close``, and
check-submit closes its planner inputs at the end of a ``with`` block. A
close that fails, as NFS and some quota setups report a lost write, raises
``OSError`` there, and the guard reports its text. The cases run each
program under ``tests/fs_fail.c``, which lets the first close of the named
file go through and then returns EIO from it. Each program runs on a site
of its own, and the two are compared on exit status, standard output, and
standard error. The tracer needs gcc, ptrace and x86_64, and the cases skip
without them. The comparison skips when TOOL_BIN is unset.

An interrupted close is a failure too. CPython's ``os.close`` does not
retry EINTR, because PEP 475 leaves close out, so it raises
``InterruptedError``. The EINTR cases also cover the scoped cpuinfo copy,
which the guard closes with ``os.close`` and skips the decision on.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import differential  # noqa: E402
import harness  # noqa: E402
from test_cpuinfo_tempdir_parity import build as cpuinfo_build  # noqa: E402
from test_restore_script_write_parity import build as restore_build  # noqa: E402
from test_restore_script_write_parity import request as restore_request  # noqa: E402
from test_submit_write_errno_parity import ARGS as SUBMIT_ARGS  # noqa: E402
from test_submit_write_errno_parity import build as submit_build  # noqa: E402

FS_FAIL = Path(__file__).resolve().parent / "fs_fail.c"
EIO = "5"
EIO_TEXT = "[Errno 5] Input/output error"
EINTR = "4"
EINTR_TEXT = "[Errno 4] Interrupted system call"
RESTORE_FILES = ("fragment.sh", "apply.sh")
SUBMIT_FILES = ("loaded.yml", "installed.txt", "unsupported.toml")
CPUINFO_COPY = "eessi-arch-guard-cpu-"
CPUINFO_MODES: Sequence[Sequence[str]] = ((), ("--json",), ("--shell",))


def _ptrace_problem() -> Optional[str]:
    """Why a child cannot be traced here, or None when it can."""
    libc = ctypes.CDLL(None, use_errno=True)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        if libc.ptrace(0, 0, None, None) != 0:
            os.write(write_fd, os.strerror(ctypes.get_errno()).encode())
            os._exit(1)
        os._exit(0)
    os.close(write_fd)
    _, status = os.waitpid(pid, 0)
    detail = os.read(read_fd, 200).decode()
    os.close(read_fd)
    if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
        return None
    return detail or "ptrace(PTRACE_TRACEME) failed with wait status %d" % status


def _build_tracer(directory: str) -> str:
    """Build tests/fs_fail.c in ``directory``, or raise SkipTest."""
    if platform.machine() != "x86_64":
        raise unittest.SkipTest("tests/fs_fail.c traces x86_64 system calls only")
    if shutil.which("gcc") is None:
        raise unittest.SkipTest("gcc is not on PATH, so tests/fs_fail.c cannot be built")
    problem = _ptrace_problem()
    if problem:
        raise unittest.SkipTest("ptrace is unavailable: %s" % problem)
    out = os.path.join(directory, "fs_fail")
    built = subprocess.run(
        ["gcc", "-O2", "-Wall", "-Wextra", "-o", out, str(FS_FAIL)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if built.returncode != 0:
        detail = (built.stderr or built.stdout or "gcc failed").strip()
        raise unittest.SkipTest("gcc could not build tests/fs_fail.c: %s" % detail)
    return out


class WriteCloseParityTests(unittest.TestCase):
    tracer = ""

    @classmethod
    def setUpClass(cls) -> None:
        build_root = tempfile.TemporaryDirectory(prefix="eag-fs-fail-")
        cls.addClassCleanup(build_root.cleanup)
        cls.tracer = _build_tracer(build_root.name)

    def failing_close(self, name: str, program: Sequence[str], errno: str = EIO) -> Sequence[str]:
        return [self.tracer, errno, "close", "1", name, "--", *program]

    def interrupted(
        self,
        name: str,
        program: Sequence[str],
        build: differential.Build,
        args: Sequence[str],
        input_for: Optional[differential.InputFor] = None,
    ) -> differential.Result:
        return differential.run_fresh(
            self.failing_close(name, program, EINTR),
            build,
            args,
            input_for=input_for,
        )

    def test_python_helper_reports_the_close_errno(self) -> None:
        for name in RESTORE_FILES:
            with self.subTest(name=name):
                run = differential.run_fresh(
                    self.failing_close(name, differential.python_guard()),
                    restore_build,
                    ["--restore"],
                    input_for=restore_request,
                )
                self.assertEqual(run.returncode, 1, run.stdout)
                self.assertEqual(run.stdout, "")
                self.assertEqual(run.stderr, "eessi-arch-guard: restore failed: %s\n" % EIO_TEXT)

    def test_programs_agree_when_a_restore_script_close_fails(self) -> None:
        harness.skip_unless_tool_bin(self)
        for name in RESTORE_FILES:
            with self.subTest(name=name):
                python = differential.run_fresh(
                    self.failing_close(name, differential.python_guard()),
                    restore_build,
                    ["--restore"],
                    input_for=restore_request,
                )
                zig = differential.run_fresh(
                    self.failing_close(name, differential.zig_program()),
                    restore_build,
                    ["--restore"],
                    input_for=restore_request,
                )
                self.assertEqual(zig.observed(), python.observed())

    def test_python_check_submit_reports_the_close_errno(self) -> None:
        for name in SUBMIT_FILES:
            with self.subTest(name=name):
                run = differential.run_fresh(
                    self.failing_close(name, differential.python_guard()),
                    submit_build,
                    SUBMIT_ARGS,
                )
                self.assertEqual(run.returncode, 2, run.stderr)
                self.assertIn("could not run eb-stack: %s" % EIO_TEXT, run.stderr)

    def test_programs_agree_when_a_planner_close_fails(self) -> None:
        harness.skip_unless_tool_bin(self)
        for name in SUBMIT_FILES:
            with self.subTest(name=name):
                python = differential.run_fresh(
                    self.failing_close(name, differential.python_guard()),
                    submit_build,
                    SUBMIT_ARGS,
                )
                zig = differential.run_fresh(
                    self.failing_close(name, differential.zig_program()),
                    submit_build,
                    SUBMIT_ARGS,
                )
                self.assertEqual(zig.observed(), python.observed())

    def test_python_raises_on_an_interrupted_close(self) -> None:
        python = differential.python_guard()
        for name in RESTORE_FILES:
            with self.subTest(name=name):
                run = self.interrupted(name, python, restore_build, ["--restore"], restore_request)
                self.assertEqual(
                    run.observed(),
                    (1, "", "eessi-arch-guard: restore failed: %s\n" % EINTR_TEXT),
                )
        for name in SUBMIT_FILES:
            with self.subTest(name=name):
                run = self.interrupted(name, python, submit_build, SUBMIT_ARGS)
                self.assertEqual(run.returncode, 2, run.stderr)
                self.assertIn("could not run eb-stack: %s" % EINTR_TEXT, run.stderr)
        for mode in CPUINFO_MODES:
            with self.subTest(name=CPUINFO_COPY, mode=" ".join(mode)):
                run = self.interrupted(CPUINFO_COPY, python, cpuinfo_build, mode)
                text = run.stdout + run.stderr
                self.assertEqual(run.returncode, 0, text)
                self.assertIn("InterruptedError", text)
                self.assertIn(EINTR_TEXT, text)
                self.assertNotIn("reloaded modules", text)

    def test_programs_agree_when_a_close_is_interrupted(self) -> None:
        harness.skip_unless_tool_bin(self)
        cases = [(name, restore_build, ["--restore"], restore_request) for name in RESTORE_FILES]
        cases += [(name, submit_build, SUBMIT_ARGS, None) for name in SUBMIT_FILES]
        cases += [(CPUINFO_COPY, cpuinfo_build, mode, None) for mode in CPUINFO_MODES]
        for name, build, args, input_for in cases:
            with self.subTest(name=name, args=" ".join(args)):
                python = self.interrupted(name, differential.python_guard(), build, args, input_for)
                zig = self.interrupted(name, differential.zig_program(), build, args, input_for)
                self.assertEqual(zig.observed(), python.observed())


if __name__ == "__main__":
    unittest.main()
