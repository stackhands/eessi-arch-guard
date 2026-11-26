"""Run the binary on a site of its own, and compare with an expected output.

A site holds the fake EESSI tree and the fake Lmod's event log. Two runs
that share one site share that log, and the second reads what the first
wrote. A file-size limit makes the effect visible: the log is capped, so the
run that comes second reaches the cap sooner and fails in a way the first
did not. A case that gives each run a site of its own sees only the program.

``run_fresh`` builds a site with a caller-supplied function, runs the program
on it, and removes it. The text of a run is normalized: the random workspace
names become ``RND`` and the site directory becomes ``@WORK@``.
``assert_golden`` compares exit status and both streams of one run with a
section of a golden file (``tests/golden.py``).
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Callable, Mapping, NamedTuple, Optional, Sequence, Tuple

import golden
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
Build = Callable[[harness.Site], Mapping[str, str]]
InputFor = Callable[[Mapping[str, str]], str]


class Result(NamedTuple):
    """What one program did on its own site."""

    returncode: int
    stdout: str
    stderr: str
    site_root: str
    log_bytes: int

    def observed(self) -> Tuple[int, str, str]:
        """Exit status and streams."""
        return self.returncode, self.stdout, self.stderr


def normalize(text: str) -> str:
    """``text`` with each random workspace name replaced by ``RND``."""
    return golden.normalize(text)


def zig_program() -> Sequence[str]:
    return [str(harness.PROGRAM)]


def run_fresh(
    program: Sequence[str],
    build: Build,
    args: Sequence[str] = (),
    fsize: Optional[int] = None,
    input_for: Optional[InputFor] = None,
) -> Result:
    """Run ``program`` once on a site made by ``build``, and remove the site.

    ``build`` receives the new site, adds what the case needs, and returns
    the environment of the run. It is called once per program and must not
    depend on state outside the site. ``fsize`` caps the size of any file the
    program writes, in bytes. ``input_for`` receives that environment and
    returns the text fed to the program, such as a restore request that names
    paths of the site.
    """
    site = harness.Site()
    try:
        env = dict(build(site))
        argv = [*program, *args]
        if fsize is not None:
            argv = [sys.executable, "-c", SIZE_LIMITER, str(fsize), *argv]
        stdin = input_for(env) if input_for is not None else None
        proc = harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT, input=stdin)
        log_bytes = os.path.getsize(site.log) if os.path.exists(site.log) else 0
        work = [site.root, os.path.realpath(site.root)]
        return Result(
            proc.returncode,
            golden.normalize(proc.stdout, work),
            golden.normalize(proc.stderr, work),
            site.root,
            log_bytes,
        )
    finally:
        site.close()


def assert_golden(
    case: unittest.TestCase,
    sheet: golden.Sheet,
    label: str,
    build: Build,
    args: Sequence[str] = (),
    fsize: Optional[int] = None,
    input_for: Optional[InputFor] = None,
) -> Result:
    """Run the program on a fresh site and check its output against ``sheet``."""
    run = run_fresh(zig_program(), build, args, fsize, input_for)
    sheet.check(case, label, golden.render(run.returncode, run.stdout, run.stderr))
    return run
