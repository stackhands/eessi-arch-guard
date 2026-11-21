"""Run the Python guard and the Zig program on separate fresh sites.

A site holds the fake EESSI tree and the fake Lmod's event log. Two programs
that share one site share that log, and the second reads what the first
wrote. A file-size limit makes the effect visible: the log is capped, so the
program that runs second reaches the cap sooner and fails in a way the first
did not. A comparison that gives each program a site of its own sees only the
difference between the programs.

``run_fresh`` builds a site with a caller-supplied function, runs one program
on it, and removes it. ``run_pair`` does that once for the Python guard and
once for the program named by ``TOOL_BIN``. The text of both runs is
normalized for the random workspace names, which differ from run to run.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from typing import Callable, Mapping, NamedTuple, Optional, Sequence, Tuple

import harness

RUN_TIMEOUT = 30.0
# Starts the program under RLIMIT_FSIZE. The soft and hard limits are the
# same, so the program cannot lift the cap. Python starts with SIGXFSZ
# ignored and exec keeps it ignored, so a write past the cap fails with
# EFBIG instead of ending the program. Run it as
# [sys.executable, "-c", SIZE_LIMITER, str(limit), *argv].
SIZE_LIMITER = (
    "import os, resource, sys\n"
    "limit = int(sys.argv[1])\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))\n"
    "os.execv(sys.argv[2], sys.argv[2:])\n"
)
_RANDOM_NAME = re.compile(r"eessi-arch-guard-(?:cpu-)?[a-z0-9_]{8}")

Build = Callable[[harness.Site], Mapping[str, str]]


class Result(NamedTuple):
    """What one program did on its own site."""

    returncode: int
    stdout: str
    stderr: str
    site_root: str
    log_bytes: int

    def observed(self) -> Tuple[int, str, str]:
        """Exit status and streams, the part two programs must share."""
        return self.returncode, self.stdout, self.stderr


def normalize(text: str) -> str:
    """``text`` with each random workspace name replaced by ``RND``."""
    return _RANDOM_NAME.sub("RND", text)


def python_guard() -> Sequence[str]:
    return [sys.executable, "-I", str(harness.GUARD)]


def zig_program() -> Sequence[str]:
    return [str(harness.PROGRAM)]


def run_fresh(
    program: Sequence[str],
    build: Build,
    args: Sequence[str] = (),
    fsize: Optional[int] = None,
) -> Result:
    """Run ``program`` once on a site made by ``build``, and remove the site.

    ``build`` receives the new site, adds what the case needs, and returns
    the environment of the run. It is called once per program and must not
    depend on state outside the site. ``fsize`` caps the size of any file the
    program writes, in bytes.
    """
    site = harness.Site()
    try:
        env = dict(build(site))
        argv = [*program, *args]
        if fsize is not None:
            argv = [sys.executable, "-c", SIZE_LIMITER, str(fsize), *argv]
        proc = harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT)
        log_bytes = os.path.getsize(site.log) if os.path.exists(site.log) else 0
        return Result(
            proc.returncode,
            normalize(proc.stdout),
            normalize(proc.stderr),
            site.root,
            log_bytes,
        )
    finally:
        site.close()


def run_pair(
    build: Build,
    args: Sequence[str] = (),
    fsize: Optional[int] = None,
) -> Tuple[Result, Result]:
    """The Python guard and the ``TOOL_BIN`` program, each on a fresh site."""
    python = run_fresh(python_guard(), build, args, fsize)
    zig = run_fresh(zig_program(), build, args, fsize)
    return python, zig


def assert_agree(
    case: unittest.TestCase,
    build: Build,
    args: Sequence[str] = (),
    fsize: Optional[int] = None,
) -> Tuple[Result, Result]:
    """Fail unless both programs exit and print alike. Returns both results."""
    python, zig = run_pair(build, args, fsize)
    case.assertEqual(zig.returncode, python.returncode, zig.stderr)
    case.assertEqual(zig.stderr, python.stderr)
    case.assertEqual(zig.stdout, python.stdout)
    return python, zig
