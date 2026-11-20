"""The Python and Zig guards must print one JSON object for an OSError skip.

An OSError the decision does not catch ends the run as a skip. With --json
the guard prints a failed decision whose cause is the exception class and
whose message is the exception text; the detail is null, because the text is
already the message. The cache directory has mode 000, so the guard decides
without the cache, and at the deadline its read of the cache opens the
directory outside any handler and raises PermissionError. For root the
guard runs without the two capabilities that let it open the directory
anyway.

The deadline comes from an event, not from a wait: the GPU query is a
script that blocks on a lock the test holds, so the decision is still
running at the deadline of a short --timeout however fast the program is.
After the run the test releases the lock, and the script takes it and exits,
whenever it gets there. The script holds the write end of a FIFO open from
its start, and the test waits for the hangup on the read end, which comes
when the script has exited. The programs are compared on exit status,
standard output, and standard error. The comparison skips when TOOL_BIN is
unset.
"""

from __future__ import annotations

import fcntl
import json
import os
import select
import signal
import sys
import unittest
from pathlib import Path
from typing import List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

CPU = "x86_64/amd/zen4"
RUN_TIMEOUT = 30.0
DECISION_TIMEOUT = "0.05"
# Root opens a directory of mode 000 through these two capabilities. With
# neither in the bounding or the inheritable set, root's program has neither
# after exec.
ROOT_WITHOUT_DAC = [
    "setpriv",
    "--inh-caps=-dac_override,-dac_read_search",
    "--bounding-set=-dac_override,-dac_read_search",
]
# The FIFO is open for writing before the pid is written, so a pid file means
# the read end of the test sees a hangup once the script and flock are gone.
BLOCKING_GPU_QUERY = (
    "#!/bin/sh\n"
    'exec 9> "$EESSI_TEST_ALIVE_FIFO"\n'
    'printf \'%s\\n\' "$$" > "$EESSI_TEST_HANG_PID"\n'
    'exec flock "$EESSI_TEST_GPU_LOCK" true\n'
)


class JsonOsSkipParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        self.site.add_cpu(CPU)
        self.site.add_module(CPU, "R/4.4.2")
        self.site.write_cpuinfo("zen4")
        self.cache = os.path.join(self.site.root, "cache")
        os.mkdir(self.cache, 0)
        self.addCleanup(os.chmod, self.cache, 0o700)
        query = Path(self.site.bindir, "nvidia-smi")
        query.write_text(BLOCKING_GPU_QUERY, encoding="ascii")
        query.chmod(0o755)
        self.lock = os.path.join(self.site.root, "gpu.lock")
        self.alive = os.path.join(self.site.root, "alive.fifo")
        os.mkfifo(self.alive, 0o600)
        self.hang_pid = os.path.join(self.site.root, "hang.pid")

    def _wait_for_exit(self, alive: int) -> None:
        """Wait for a GPU query that started to exit, once its lock is free."""
        try:
            pid = int(Path(self.hang_pid).read_text(encoding="ascii"))
        except (OSError, ValueError):
            return
        os.unlink(self.hang_pid)
        ready, _, _ = select.select([alive], [], [], RUN_TIMEOUT)
        if not ready:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.fail("the GPU query did not exit after its lock was released")

    def _run(self, guard: Sequence[str]):
        env = harness.base_env(self.site, CPU, ["R/4.4.2"], gpu="1", gpu_cc="8.0")
        env["TMPDIR"] = self.cache
        env["SLURM_JOB_ID"] = "6060"
        env["SLURM_STEP_ID"] = "batch"
        env["EESSI_TEST_HANG_PID"] = self.hang_pid
        env["EESSI_TEST_GPU_LOCK"] = self.lock
        env["EESSI_TEST_ALIVE_FIFO"] = self.alive
        argv: List[str] = [*guard, "--timeout", DECISION_TIMEOUT, "--json"]
        if os.geteuid() == 0:
            argv = [*ROOT_WITHOUT_DAC, *argv]
        alive = os.open(self.alive, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with open(self.lock, "w", encoding="ascii") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                proc = harness.run_with_timeout(argv, env=env, timeout=RUN_TIMEOUT)
            self._wait_for_exit(alive)
            return proc
        finally:
            os.close(alive)

    def test_python_guard_prints_the_error_without_a_detail(self) -> None:
        proc = self._run([sys.executable, "-I", str(harness.GUARD)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["cause"], "PermissionError")
        self.assertEqual(
            report["message"], "[Errno 13] Permission denied: %r" % self.cache
        )
        self.assertIsNone(report["detail"])

    def test_programs_agree_on_the_json_of_an_os_error_skip(self) -> None:
        harness.skip_unless_tool_bin(self)
        python = self._run([sys.executable, "-I", str(harness.GUARD)])
        program = self._run([str(harness.PROGRAM)])
        self.assertEqual(program.returncode, python.returncode)
        self.assertEqual(program.stderr, python.stderr)
        self.assertEqual(
            json.loads(program.stdout), json.loads(python.stdout), program.stdout
        )


if __name__ == "__main__":
    unittest.main()
