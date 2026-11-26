"""End-to-end runs against a fake EESSI tree, archdetect stub, and LMOD_CMD.

No real Lmod, Slurm, nvidia-smi, or CVMFS. Targets are faked with
EESSI_PROC_CPUINFO and a nvidia-smi stub that is first on PATH.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import NamedTuple, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import golden  # noqa: E402
import harness  # noqa: E402
from slurm_model import apply_task_prolog  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"
CC90 = "accel/nvidia/cc90"
CC120 = "accel/nvidia/cc120"

STRICT_FAILURE_EXIT = 78
# The longest a stopped decision waits for each of its children after a kill.
_POST_KILL_WAIT_SECONDS = 5.0
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CACHE_NAME_RE = re.compile(
    r"\.eessi-arch-guard\.[0-9]+\.[A-Za-z0-9._-]+\.(prolog|shell)\.[0-9a-f]{16}"
)
_VERSION_RE = re.compile(r'^\s*\.version = "([^"]+)"', re.M)


class _Outcome(NamedTuple):
    """The content of a decision cache file."""

    changes: dict
    message: Optional[str]
    kind: str
    detail: Optional[str] = None


def _one_line(text: str, limit: int = 500) -> str:
    """``text`` on one line, controls as spaces, bounded to ``limit`` characters."""
    chars = [" " if ch in "\r\n\0" or ord(ch) < 32 or ord(ch) == 127 else ch for ch in text]
    cleaned = "".join(chars).strip()
    if len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned


def _cache_path(env, shell=False):
    """The cache file the program names for ``env``, as its --dry-run --json reports it."""
    quiet = {
        k: v for k, v in env.items() if k not in ("EESSI_TEST_GPU_HANG", "EESSI_TEST_LMOD_HANG")
    }
    proc = harness.run_guard(quiet, ["--dry-run", "--json"] + (["--shell"] if shell else []))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["cache"]["path"]


def _write_cache(path, outcome):
    """Write a private cache file with the content of ``outcome``."""
    payload = {
        "v": 1,
        "kind": outcome.kind,
        "message": outcome.message,
        "detail": outcome.detail,
        "changes": outcome.changes,
    }
    data = json.dumps(payload, ensure_ascii=True).encode("ascii") + b"\n"
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _cgroup_cpus():
    """The processors the cgroup v2 cpuset of this process allows, or None."""
    try:
        with open("/proc/self/cgroup", encoding="ascii") as handle:
            line = next(entry for entry in handle if entry.startswith("0::"))
        directory = "/sys/fs/cgroup" + line.rstrip("\n")[3:]
        with open(directory + "/cpuset.cpus.effective", encoding="ascii") as handle:
            spec = handle.read().strip()
    except (OSError, StopIteration):
        return None
    cpus = set()
    for part in filter(None, spec.split(",")):
        low, _, high = part.partition("-")
        cpus.update(range(int(low), int(high or low) + 1))
    return cpus or None


def _program_bytes() -> bytes:
    return harness.PROGRAM.read_bytes()


def _program_version() -> str:
    zon = harness.ROOT / "build.zig.zon"
    return _VERSION_RE.search(zon.read_text(encoding="utf-8")).group(1)


def _site():
    site = harness.Site()
    for cpu in (ZEN2, ZEN4):
        site.add_cpu(cpu)
        site.add_module(cpu, "R/4.4.2")
    for accel in (CC90, CC120):
        site.add_accel(ZEN4, accel)
        version = "12.6.0" if accel == CC90 else "12.8.0"
        site.add_module(ZEN4, "CUDA/" + version, accel=accel)
    return site


def _assert_prolog(proc, code=0):
    assert proc.returncode == code, (proc.returncode, proc.stdout, proc.stderr)
    for line in proc.stdout.split("\n"):
        if line == "":
            continue
        assert line.startswith(("export ", "unset ", "print ")), line
    changes, printed = apply_task_prolog(proc.stdout)
    for key, value in changes.items():
        assert _ENV_NAME_RE.fullmatch(key), key
        if value is not None:
            assert "\n" not in value and "\r" not in value
    return changes, printed


def _stop_limit(timeout: float) -> float:
    """Decision timeout plus the worst-case post-kill waits."""
    return timeout + 3 * _POST_KILL_WAIT_SECONDS


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_until_dead(pid: int) -> None:
    for _ in range(50):
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)


def _apply(env, changes):
    updated = dict(env)
    for key, value in changes.items():
        if value is None:
            updated.pop(key, None)
        else:
            updated[key] = value
    return updated


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.site = _site()

    def tearDown(self):
        self.site.close()

    def _cpu_env(self, loaded, node, modules=("R/4.4.2",)):
        self.site.write_cpuinfo(node)
        return harness.base_env(self.site, loaded, list(modules), gpu="none")

    def test_zen4_to_zen2(self):
        env = self._cpu_env(ZEN4, "zen2")
        proc = harness.run_guard(env)
        changes, printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2)
        self.assertIn(ZEN2, changes["EBROOTR"])
        self.assertNotIn(ZEN4, changes["EBROOTR"])
        self.assertEqual(changes["EESSI_ARCH_GUARD_RELOADED"], "%s->%s" % (ZEN4, ZEN2))
        self.assertTrue(printed)
        self.assertIn("--force", " ".join(harness.log_lines(self.site.log)))
        # Idempotent once the task environment is the reloaded one.
        again = harness.run_guard(_apply(env, changes))
        self.assertEqual(again.stdout, "")
        self.assertEqual(again.returncode, 0)

    def test_zen2_to_zen4(self):
        env = self._cpu_env(ZEN2, "zen4")
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN4)
        self.assertIn(ZEN4, changes["EBROOTR"])
        self.assertEqual(changes["EESSI_ARCH_GUARD_RELOADED"], "%s->%s" % (ZEN2, ZEN4))

    def test_cc90_to_cc120(self):
        self.site.write_cpuinfo("zen4")
        env = harness.base_env(
            self.site, ZEN4, ["R/4.4.2", "CUDA/12.6.0"], accel=CC90, gpu="1", gpu_cc="12.0"
        )
        # CUDA/12.6.0 is not on cc120; the reload must fail rather than half-load
        # if that module is absent. Put the same module name on cc120 too so the
        # target change itself can succeed, and check the path moved.
        self.site.add_module(ZEN4, "CUDA/12.6.0", accel=CC120)
        proc = harness.run_guard(env)
        changes, printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_ACCEL_SUBDIR"], CC120)
        self.assertIn(CC120, changes["MODULEPATH"])
        self.assertIn(CC90, changes["EESSI_ARCH_GUARD_RELOADED"])
        self.assertIn(CC120, changes["EESSI_ARCH_GUARD_RELOADED"])
        self.assertTrue(any("cc120" in line or "cc90" in line for line in printed))

    def test_no_gpu_to_gpu(self):
        self.site.write_cpuinfo("zen4")
        self.site.add_module(ZEN4, "CUDA/12.8.0", accel=CC120)
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="1", gpu_cc="12.0")
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes.get("EESSI_ACCEL_SUBDIR"), CC120)
        self.assertIn("no accel", changes["EESSI_ARCH_GUARD_RELOADED"])
        self.assertIn(CC120, changes["EESSI_ARCH_GUARD_RELOADED"])

    def test_gpu_to_no_gpu(self):
        self.site.write_cpuinfo("zen4")
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], accel=CC90, gpu="none")
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes.get("EESSI_ACCEL_SUBDIR"), "")
        self.assertIn("no accel", changes["EESSI_ARCH_GUARD_RELOADED"])
        self.assertNotIn(CC90, changes.get("MODULEPATH", env["MODULEPATH"]))

    def test_accel_pinned_is_a_noop(self):
        self.site.write_cpuinfo("zen4")
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], accel=CC90, gpu="1", gpu_cc="12.0")
        env["EESSI_ACCELERATOR_TARGET_OVERRIDE"] = CC90
        proc = harness.run_guard(env)
        _assert_prolog(proc)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(harness.log_lines(self.site.log), [])

    def test_same_target_noop(self):
        self.site.write_cpuinfo("zen4")
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        proc = harness.run_guard(env)
        _assert_prolog(proc)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(harness.log_lines(self.site.log), [])
        self.assertEqual(harness.log_lines(self.site.arch_log), ["cpupath", "accelpath"])

    def test_unsafe_software_override_is_ignored(self):
        env = self._cpu_env(ZEN4, "zen2")
        env["EESSI_SOFTWARE_SUBDIR_OVERRIDE"] = "../../oops"
        proc = harness.run_guard(env)
        changes, printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2)
        self.assertIn("EESSI_ARCH_GUARD_RELOADED", changes)
        self.assertTrue(printed)

    def test_missing_software_dir_is_loud_and_configurable(self):
        bare = harness.Site()
        try:
            bare.write_cpuinfo("zen2")
            env = harness.base_env(bare, ZEN4, [], gpu="none")
            # No module files were added; the software directory itself is absent.
            proc = harness.run_guard(env)
            changes, printed = _assert_prolog(proc)
            self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->no-software-dir"))
            self.assertNotIn("EESSI_SOFTWARE_SUBDIR", changes)
            self.assertTrue(printed)
            self.assertIn("no EESSI software directory", printed[0])
            env["EESSI_ARCH_GUARD_MISSING"] = "ignore"
            quiet = harness.run_guard(env)
            _assert_prolog(quiet)
            self.assertEqual(quiet.stdout, "")
        finally:
            bare.close()

    def test_tmpdir_absent_uses_tmp_mode_0600(self):
        self.site.write_cpuinfo("zen2")
        env = self._cpu_env(ZEN4, "zen2")
        env["TMPDIR"] = os.path.join(self.site.root, "not-created-yet")
        # The cache lands in the shared /tmp, so the job is this process alone.
        env["SLURM_JOB_ID"] = "4242%d" % os.getpid()
        env["SLURM_STEP_ID"] = "batch"
        path = _cache_path(env)
        self.assertIsNotNone(path)
        self.assertTrue(path.startswith("/tmp/"))
        old = os.umask(0)
        try:
            proc = harness.run_guard(env)
        finally:
            os.umask(old)
        try:
            _assert_prolog(proc)
            self.assertTrue(os.path.isfile(path))
            self.assertFalse(os.path.islink(path))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(path + ".lock").st_mode & 0o777, 0o600)
        finally:
            for suffix in ("", ".lock"):
                try:
                    os.unlink(path + suffix)
                except OSError:
                    pass

    def test_restore_that_cannot_run_names_its_cause(self):
        env = self._cpu_env(ZEN4, "zen2")
        shared = os.path.join(self.site.root, "shared")
        os.mkdir(shared)
        os.chmod(shared, 0o755)
        env["EESSI_ARCH_GUARD_WORKDIR"] = shared
        proc = harness.run_guard(env)
        changes, printed = _assert_prolog(proc)
        self.assertEqual(changes, {"EESSI_ARCH_GUARD_FAILED": "%s->restore-failed" % ZEN4})
        self.assertEqual(
            printed,
            [
                "eessi-arch-guard: restore failed (restore workspace unavailable: "
                "EESSI_ARCH_GUARD_WORKDIR is not an existing private owned directory); "
                "environment left as it was"
            ],
        )
        self.assertEqual(harness.log_lines(self.site.log), [])

        # Strict mode with neither a private TMPDIR nor a state directory.
        del env["EESSI_ARCH_GUARD_WORKDIR"]
        env["TMPDIR"] = shared
        env["HOME"] = os.path.join(self.site.root, "home")
        proc = harness.run_guard(env, ["--strict", "--shell"])
        self.assertEqual(proc.returncode, STRICT_FAILURE_EXIT, proc.stderr)
        self.assertIn("'%s->restore-failed'" % ZEN4, proc.stdout)
        self.assertEqual(
            proc.stderr,
            "eessi-arch-guard: restore failed (restore workspace unavailable: "
            "strict mode needs an existing private job or state workspace); "
            "environment left as it was\n",
        )
        self.assertEqual(harness.log_lines(self.site.log), [])

    def test_cache_symlink_is_not_followed(self):
        self.site.write_cpuinfo("zen2")
        env = self._cpu_env(ZEN4, "zen2")
        env["TMPDIR"] = self.site.root
        env["SLURM_JOB_ID"] = "515151"
        path = _cache_path(env)
        canary = path + ".canary"
        with open(canary, "w", encoding="utf-8") as handle:
            handle.write(
                '{"v":1,"kind":"reloaded","message":null,"detail":null,'
                '"changes":{"LD_PRELOAD":"/evil.so"}}'
            )
        os.symlink(canary, path)
        proc = harness.run_guard(env)
        try:
            changes, _printed = _assert_prolog(proc)
            self.assertNotIn("LD_PRELOAD", changes)
            self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2)
            with open(canary, encoding="utf-8") as handle:
                body = handle.read()
            self.assertTrue(body.startswith("{"))
            self.assertIn("/evil.so", body)
            self.assertFalse(os.path.islink(path))
        finally:
            for item in (path, path + ".lock", canary):
                try:
                    os.unlink(item)
                except OSError:
                    pass

    def test_bash_env_is_not_sourced(self):
        script = os.path.join(self.site.root, "bash_env.sh")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/bin/bash\n"
                "echo BASH_ENV_RAN >> %s\n"
                "echo INJECTED_STDOUT\n" % self.site.bash_env_log
            )
        os.chmod(script, 0o755)
        env = self._cpu_env(ZEN4, "zen2")
        env["BASH_ENV"] = script
        env["EESSI_TEST_SENTINEL"] = 'cost $5 `id` "q"'
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertFalse(os.path.exists(self.site.bash_env_log))
        self.assertNotIn("INJECTED_STDOUT", proc.stdout)
        self.assertNotIn("BASH_ENV_RAN", proc.stdout)
        self.assertEqual(changes["ROUNDTRIP"], 'cost $5 `id` "q"')
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2)

    def test_newline_module_name_is_not_an_export(self):
        env = self._cpu_env(ZEN4, "zen2")
        env["LOADEDMODULES"] = "EESSI/2025.06:R/4.4.2\nexport LD_PRELOAD=evil"
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertNotIn("LD_PRELOAD", changes)
        self.assertNotIn("EESSI_SOFTWARE_SUBDIR", changes)
        self.assertIn("unsafe-module-name", changes["EESSI_ARCH_GUARD_FAILED"])
        self.assertEqual(harness.log_lines(self.site.log), [])

    def test_newline_value_aborts_without_partial_apply(self):
        env = self._cpu_env(ZEN4, "zen2")
        env["EESSI_TEST_INJECT_NEWLINE"] = "1"
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertNotIn("EVIL", changes)
        self.assertNotIn("UNSAFE_NEWLINE", changes)
        self.assertNotIn("EESSI_SOFTWARE_SUBDIR", changes)
        self.assertIn("unsafe-environment", changes["EESSI_ARCH_GUARD_FAILED"])

    def test_missing_modules_are_all_named(self):
        self.site.add_module(ZEN4, "OnlyZen4/1.0")
        env = self._cpu_env(ZEN4, "zen2", modules=("R/4.4.2", "OnlyZen4/1.0"))
        # R exists on zen2; OnlyZen4 does not. Remove R from zen2 as well so both fail.
        shutil.rmtree(os.path.join(self.site.prefix, "software", "linux", ZEN2, "modules"))
        os.makedirs(os.path.join(self.site.prefix, "software", "linux", ZEN2, "modules", "all"))
        proc = harness.run_guard(env)
        changes, printed = _assert_prolog(proc)
        self.assertNotIn("EESSI_SOFTWARE_SUBDIR", changes)
        self.assertIn("R/4.4.2", printed[0])
        self.assertIn("OnlyZen4/1.0", printed[0])
        self.assertIn("environment left as it was", printed[0])

    def test_module_exposed_by_an_earlier_module_loads_after_it(self):
        # EESSI-extend puts the user's installation tree on MODULEPATH. One
        # `module load` call finds every name before it loads any, so a
        # module from that tree loads only after EESSI-extend has.
        extend = "EESSI-extend/2025.06-easybuild"
        pigz = "pigz/2.8-GCCcore-14.2.0"
        user = {}
        for cpu in (ZEN2, ZEN4):
            user[cpu] = os.path.join(self.site.root, "user", cpu, "modules", "all")
            self.site.add_module(cpu, extend)
            modulefile = os.path.join(
                self.site.prefix, "software", "linux", cpu, "modules", "all", extend + ".lua"
            )
            with open(modulefile, "a", encoding="utf-8") as handle:
                handle.write('prepend_path("MODULEPATH", "%s")\n' % user[cpu])
            os.makedirs(os.path.join(user[cpu], "pigz"))
            with open(os.path.join(user[cpu], pigz + ".lua"), "w", encoding="utf-8") as handle:
                handle.write("-- %s\n" % pigz)
        env = self._cpu_env(ZEN4, "zen2", modules=(extend, pigz))
        env["MODULEPATH"] = user[ZEN4] + ":" + env["MODULEPATH"]
        proc = harness.run_guard(env)
        changes, printed = _assert_prolog(proc)
        self.assertNotIn("EESSI_ARCH_GUARD_FAILED", changes)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2)
        self.assertEqual(changes["MODULEPATH"].split(":")[0], user[ZEN2])
        self.assertNotIn(ZEN4, changes["MODULEPATH"])
        self.assertIn(ZEN2, changes["EBROOTPIGZ"])
        self.assertTrue(printed)

    def test_missing_module_after_the_first_32_is_named(self):
        names = ["P%02d/1" % index for index in range(40)]
        for name in names:
            self.site.add_module(ZEN4, name)
            if name != names[35]:
                self.site.add_module(ZEN2, name)
        env = self._cpu_env(ZEN4, "zen2", modules=names)
        proc = harness.run_guard(env)
        changes, printed = _assert_prolog(proc)
        self.assertNotIn("EESSI_SOFTWARE_SUBDIR", changes)
        self.assertIn("; not available for this target: %s; " % names[35], printed[0])

    def test_missing_list_stops_after_32(self):
        names = ["P%02d/1" % index for index in range(40)]
        for name in names:
            self.site.add_module(ZEN4, name)
        env = self._cpu_env(ZEN4, "zen2", modules=names)
        proc = harness.run_guard(env)
        _changes, printed = _assert_prolog(proc)
        listed = " ".join(names[:32])
        self.assertIn("not available for this target: %s (stopped after 32);" % listed, printed[0])
        self.assertNotIn(names[32], printed[0])

    def test_stderr_injection_stays_out_of_prolog_directives(self):
        self.site.add_module(ZEN4, "OnlyZen4/1.0")
        env = self._cpu_env(ZEN4, "zen2", modules=("OnlyZen4/1.0",))
        env["EESSI_TEST_STDERR_INJECT"] = "1"
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertNotIn("PWNED", changes)
        self.assertNotIn("export PWNED=1\n", proc.stdout)

    def test_shell_mode_stdout_is_eval_safe(self):
        env = self._cpu_env(ZEN4, "zen2")
        canary = os.path.join(self.site.root, "canary")
        env["EESSI_TEST_SENTINEL"] = "$(touch %s) `touch %s`" % (canary, canary)
        proc = harness.run_guard(env, ["--shell"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("print ", proc.stdout)
        self.assertNotIn("INJECTED", proc.stdout)
        self.assertIn("eessi-arch-guard:", proc.stderr)
        check = subprocess.run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                'eval "$1"; printf %s "$EESSI_SOFTWARE_SUBDIR"; printf "\\n%s" "$ROUNDTRIP"',
                "bash",
                proc.stdout,
            ],
            capture_output=True,
            encoding="utf-8",
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
            check=False,
        )
        self.assertEqual(check.returncode, 0, check.stderr)
        subdir, sentinel = check.stdout.split("\n", 1)
        self.assertEqual(subdir, ZEN2)
        self.assertEqual(sentinel, env["EESSI_TEST_SENTINEL"])
        self.assertFalse(os.path.exists(canary))

    def test_localid_suppresses_the_print_line(self):
        env = self._cpu_env(ZEN4, "zen2")
        env["SLURM_LOCALID"] = "3"
        proc = harness.run_guard(env)
        changes, printed = _assert_prolog(proc)
        self.assertEqual(printed, [])
        self.assertIn("EESSI_ARCH_GUARD_RELOADED", changes)

    def test_localid_suppresses_the_skip_line(self):
        env = self._cpu_env(ZEN4, "zen2")
        env["TMPDIR"] = self.site.root
        env["SLURM_JOB_ID"] = "4343"
        # A cached line is not measured again, so this one is refused at
        # output and every task of the step takes the skip path.
        _write_cache(
            _cache_path(env),
            _Outcome({"SITE_DATA": "x" * 5000}, None, "reloaded", None),
        )
        printed_by_task = {}
        for localid in ("0", "1", "3"):
            env["SLURM_LOCALID"] = localid
            proc = harness.run_guard(env)
            changes, printed = _assert_prolog(proc)
            self.assertEqual(changes, {})
            printed_by_task[localid] = printed
        self.assertEqual(len(printed_by_task["0"]), 1, printed_by_task)
        self.assertTrue(
            printed_by_task["0"][0].startswith("eessi-arch-guard: skipped, ValueError: "),
            printed_by_task,
        )
        self.assertEqual(printed_by_task["1"], [])
        self.assertEqual(printed_by_task["3"], [])
        self.assertEqual(harness.log_lines(self.site.log), [])

    def test_opt_out_and_archdetect_failure(self):
        env = self._cpu_env(ZEN4, "zen2")
        env["EESSI_ARCH_GUARD"] = "0"
        proc = harness.run_guard(env)
        _assert_prolog(proc)
        self.assertEqual(proc.stdout, "")
        self.site.write_cpuinfo("zen2", fail=True)
        env = self._cpu_env(ZEN4, "zen2")
        # write_cpuinfo again inside _cpu_env; set fail after.
        self.site.write_cpuinfo("zen2", fail=True)
        proc = harness.run_guard(env)
        changes, printed = _assert_prolog(proc)
        self.assertIn("archdetect-failed", changes["EESSI_ARCH_GUARD_FAILED"])
        self.assertTrue(printed)
        self.assertEqual(harness.log_lines(self.site.log), [])

    def test_an_option_is_not_the_value_of_chain_or_timeout(self):
        # A next word that starts with "-" is an option of its own, and the
        # value is empty: --strict after --chain or --timeout still counts.
        env = self._cpu_env(ZEN4, "zen2")
        self.site.write_cpuinfo("zen2", fail=True)
        for option, warning in (
            ("--chain", "--chain is not an absolute executable"),
            ("--timeout", "--timeout is not a positive number of seconds at most 2147483.647"),
        ):
            proc = harness.run_guard(env, [option, "--strict"])
            changes, _printed = _assert_prolog(proc, code=STRICT_FAILURE_EXIT)
            self.assertIn("archdetect-failed", changes["EESSI_ARCH_GUARD_FAILED"])
            self.assertEqual(proc.stderr, "eessi-arch-guard: %s\n" % warning)
        self.assertEqual(harness.log_lines(self.site.log), [])

    def test_first_candidate_with_a_software_directory(self):
        # Drop zen4's tree so the node's best match cannot be used.
        shutil.rmtree(os.path.join(self.site.prefix, "software", "linux", ZEN4))
        self.site.write_cpuinfo(matches="x86_64/amd/zen5:%s:%s" % (ZEN4, ZEN2))
        env = harness.base_env(self.site, ZEN2, ["R/4.4.2"], gpu="none")
        # Loaded for zen2, and zen2 is the first candidate that exists: no-op
        # even though the cpuinfo prefers zen5. Then load as zen4-in-name only.
        proc = harness.run_guard(env)
        self.assertEqual(proc.stdout, "")
        env["EESSI_SOFTWARE_SUBDIR"] = ZEN4
        env["_ModuleTable001_"] = "table-for-foreign"
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2)

    def test_eight_tasks_one_reload(self):
        env = self._cpu_env(ZEN4, "zen2")
        env["TMPDIR"] = self.site.root
        env["SLURM_JOB_ID"] = "8888"
        env["SLURM_STEP_ID"] = "0"
        procs = []
        for rank in range(8):
            one = dict(env)
            one["SLURM_LOCALID"] = str(rank)
            procs.append(
                subprocess.Popen(
                    list(harness.GUARD_ARGV),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",
                    env=one,
                )
            )
        prints = 0
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
            _changes, printed = apply_task_prolog(out)
            prints += len(printed)
        loads = [
            line for line in harness.log_lines(self.site.log) if line.startswith("load EESSI/")
        ]
        self.assertEqual(loads, ["load EESSI/2025.06"])
        self.assertEqual(prints, 1)

    def test_parallel_noop_detects_once(self):
        self.site.write_cpuinfo("zen4")
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        env["TMPDIR"] = self.site.root
        env["SLURM_JOB_ID"] = "7777"
        procs = []
        for rank in range(4):
            one = dict(env)
            one["SLURM_LOCALID"] = str(rank)
            procs.append(
                subprocess.Popen(
                    list(harness.GUARD_ARGV),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",
                    env=one,
                )
            )
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
            self.assertEqual(out, "")
        self.assertEqual(harness.log_lines(self.site.log), [])
        self.assertEqual(harness.log_lines(self.site.arch_log).count("cpupath"), 1)

    def test_chain_success_and_failure(self):
        env = self._cpu_env(ZEN4, "zen2")
        ok_path = os.path.join(self.site.root, "chain-ok.sh")
        with open(ok_path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho 'export FROM_CHAIN=yes'\n")
        os.chmod(ok_path, 0o755)
        proc = harness.run_guard(env, ["--chain", ok_path])
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes["FROM_CHAIN"], "yes")
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2)

        bad_path = os.path.join(self.site.root, "chain-bad.sh")
        with open(bad_path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho 'export FROM_CHAIN=yes'\nexit 9\n")
        os.chmod(bad_path, 0o755)
        # Fresh log so a second reload would be visible.
        open(self.site.log, "w").close()
        proc = harness.run_guard(env, ["--chain", bad_path])
        self.assertEqual(proc.returncode, 9)
        self.assertIn("export FROM_CHAIN=yes", proc.stdout)
        self.assertNotIn("EESSI_ARCH_GUARD_RELOADED", proc.stdout)
        self.assertEqual(harness.log_lines(self.site.log), [])

    def test_chain_exec_failure_skips_with_errno_message(self):
        env = self._cpu_env(ZEN4, "zen2")
        bad_shebang = os.path.join(self.site.root, "chain-bad-shebang.sh")
        with open(bad_shebang, "w", encoding="utf-8") as handle:
            handle.write("#!/no/such/interpreter\nexit 0\n")
        os.chmod(bad_shebang, 0o755)
        empty_exec = os.path.join(self.site.root, "chain-empty.sh")
        with open(empty_exec, "wb") as handle:
            handle.write(b"")
        os.chmod(empty_exec, 0o755)

        cases = (
            (
                bad_shebang,
                [],
                0,
                f"print eessi-arch-guard: skipped, FileNotFoundError: "
                f"[Errno 2] No such file or directory: {bad_shebang!r}\n",
            ),
            (
                bad_shebang,
                ["--strict"],
                STRICT_FAILURE_EXIT,
                f"print eessi-arch-guard: skipped, FileNotFoundError: "
                f"[Errno 2] No such file or directory: {bad_shebang!r}\n",
            ),
            (
                empty_exec,
                [],
                0,
                f"print eessi-arch-guard: skipped, OSError: "
                f"[Errno 8] Exec format error: {empty_exec!r}\n",
            ),
        )
        for segs in (["a" * 150] * 4, ["b" * 200] * 5):
            long_path = os.path.join(self.site.root, *segs[:-1], "chain.sh")
            os.makedirs(os.path.dirname(long_path), exist_ok=True)
            shutil.copy2(bad_shebang, long_path)
            message = (
                f"eessi-arch-guard: skipped, FileNotFoundError: "
                f"[Errno 2] No such file or directory: {long_path!r}"
            )
            cases += ((long_path, [], 0, f"print {_one_line(message)}\n"),)
        for path, extra, expected_code, expected_out in cases:
            with self.subTest(path=path, extra=extra):
                proc = harness.run_guard(env, ["--chain", path, *extra])
                self.assertEqual(proc.returncode, expected_code, proc.stderr)
                self.assertEqual(proc.stdout, expected_out)
                self.assertEqual(proc.stderr, "")

    def test_json_decision_with_oversized_subdir(self):
        long_was = "a" * 5000
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "EESSI_SOFTWARE_SUBDIR": long_was}
        proc = harness.run_guard(env, ["--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        expected_msg = (
            "eessi-arch-guard: archdetect failed "
            "(EESSI_PREFIX, EESSI_OS_TYPE, or EESSI_INIT_PREFIX is missing or unsafe); "
            "environment left as it was"
        )
        expected = {
            "cached": False,
            "cause": "archdetect-failed",
            "decision": "failed",
            "detail": None,
            "exit": 0,
            "exit_semantic": "ok",
            "kind": "decision",
            "message": expected_msg,
            "now": None,
            "reload_needed": True,
            "schema_version": 1,
            "shell": False,
            "strict": False,
            "was": long_was,
        }
        self.assertEqual(
            proc.stdout,
            json.dumps(expected, ensure_ascii=True, sort_keys=True) + "\n",
        )

    def test_bad_config_byte_keeps_the_other_keys(self):
        conf = harness.PROGRAM.with_name("eessi-arch-guard.conf")
        ok_path = os.path.join(self.site.root, "chain-ok.sh")
        with open(ok_path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho 'export FROM_CHAIN=yes'\n")
        os.chmod(ok_path, 0o755)
        conf.write_bytes(
            ("chain = %s\n" % ok_path).encode("ascii") + b"# caf\xe9\ntimeout = 1\xff\n"
        )
        subprocess.check_call(["sudo", "chown", "root:root", str(conf)])
        subprocess.check_call(["sudo", "chmod", "644", str(conf)])
        try:
            env = self._cpu_env(ZEN4, "zen2")
            proc = harness.run_guard(env)
            changes, _printed = _assert_prolog(proc)
            self.assertEqual(changes["FROM_CHAIN"], "yes")
            self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2)
            self.assertIn("original bytes", proc.stderr)
            self.assertIn("timeout", proc.stderr)
        finally:
            subprocess.check_call(["sudo", "rm", "-f", str(conf)])

    def test_untrusted_config_is_ignored(self):
        conf = harness.PROGRAM.with_name("eessi-arch-guard.conf")
        conf.write_text("chain = /bin/true\n", encoding="utf-8")
        try:
            env = self._cpu_env(ZEN4, "zen2")
            proc = harness.run_guard(env)
            _assert_prolog(proc)
            self.assertIn("not owned by root", proc.stderr)
            self.assertIn("EESSI_ARCH_GUARD_RELOADED", proc.stdout)
        finally:
            conf.unlink(missing_ok=True)

    def test_config_beside_a_link_found_through_path(self):
        # The program's own path is the path execve was given, which a PATH
        # search makes the link it found: the config beside the link is the
        # one read, made absolute and normalized.
        target_dir = os.path.join(self.site.root, "target")
        link_dir = os.path.join(self.site.root, "link")
        os.makedirs(target_dir)
        os.makedirs(link_dir)
        program = os.path.join(target_dir, "guard")
        shutil.copy(harness.PROGRAM, program)
        os.chmod(program, 0o755)
        os.symlink(program, os.path.join(link_dir, "eessi-arch-guard"))
        for directory in (target_dir, link_dir):
            conf = os.path.join(directory, "eessi-arch-guard.conf")
            with open(conf, "w", encoding="utf-8") as handle:
                handle.write("timeout = 5\n")
        expected = (
            "eessi-arch-guard: config ignored: %s is not owned by root and is ignored\n"
            % os.path.join(link_dir, "eessi-arch-guard.conf")
        )
        for entry in (link_dir, "./link"):
            proc = subprocess.run(
                ["eessi-arch-guard", "--shell"],
                capture_output=True,
                encoding="utf-8",
                env={"PATH": entry + ":/usr/bin:/bin", "LC_ALL": "C"},
                cwd=self.site.root,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, (entry, proc.stderr))
            self.assertEqual(proc.stdout, "", entry)
            self.assertEqual(proc.stderr, expected, entry)

    def test_latin1_locale_keeps_bytes_through_the_restore(self):
        parent = os.path.join(tempfile.gettempdir(), "caf\udce9-eessi-arch-guard")
        os.makedirs(parent, exist_ok=True)
        locale_dir = os.path.join(parent, "locale")
        os.makedirs(locale_dir, exist_ok=True)
        subprocess.run(
            [
                "localedef",
                "-f",
                "ISO-8859-1",
                "-i",
                "en_US",
                os.path.join(locale_dir, "en_US.ISO-8859-1"),
            ],
            check=True,
            capture_output=True,
        )
        self.site.close()
        self.site = harness.Site(directory=parent)
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        env = self._cpu_env(ZEN4, "zen2")
        env["LOCPATH"] = locale_dir
        env["LC_ALL"] = "en_US.ISO-8859-1"
        env["LANG"] = "en_US.ISO-8859-1"
        proc = subprocess.run(
            list(harness.GUARD_ARGV),
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(b"module load failed", proc.stdout)
        self.assertNotIn(b"skipped", proc.stdout)
        self.assertIn(b"caf\xe9", proc.stdout)
        self.assertIn(("EESSI_SOFTWARE_SUBDIR=%s" % ZEN2).encode("ascii"), proc.stdout)

    def test_non_utf8_path_entry_keeps_its_bytes(self):
        env = self._cpu_env(ZEN4, "zen2")
        env["LC_ALL"] = "en_US.UTF-8"
        env["LANG"] = "en_US.UTF-8"
        raw = "/opt/caf\udce9/bin"
        env["PATH"] = raw + ":" + env["PATH"]
        env["EESSI_TEST_BASE_PATH"] = raw + ":" + env["EESSI_TEST_BASE_PATH"]
        proc = subprocess.run(
            list(harness.GUARD_ARGV),
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(b"UnicodeEncodeError", proc.stdout)
        self.assertNotIn(b"skipped", proc.stdout)
        self.assertNotIn(b"\xed\xb0\xa9", proc.stdout)
        self.assertIn(b"/opt/caf\xe9/bin", proc.stdout)
        self.assertIn(("EESSI_SOFTWARE_SUBDIR=%s" % ZEN2).encode("ascii"), proc.stdout)
        for args in ([], ["--shell"]):
            with open("/dev/full", "wb") as sink:
                blocked = subprocess.run(
                    [*harness.GUARD_ARGV, *args],
                    stdout=sink,
                    stderr=subprocess.PIPE,
                    env=env,
                    check=False,
                )
            self.assertEqual(blocked.returncode, 0, (args, blocked.returncode, blocked.stderr))
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            blocked = subprocess.run(
                [*harness.GUARD_ARGV, *args],
                stdout=write_fd,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )
            os.close(write_fd)
            self.assertEqual(blocked.returncode, 0, (args, blocked.returncode, blocked.stderr))

    def _hang_env(self, loaded=ZEN4, node="zen4"):
        self.site.write_cpuinfo(node)
        env = harness.base_env(self.site, loaded, ["R/4.4.2"], gpu="1", gpu_cc="8.0")
        env["TMPDIR"] = self.site.root
        env["SLURM_JOB_ID"] = "6060"
        env["SLURM_STEP_ID"] = "batch"
        env["EESSI_TEST_HANG_PID"] = os.path.join(self.site.root, "hang.pid")
        return env

    def test_hung_gpu_query_is_stopped_and_not_cached(self):
        env = self._hang_env()
        env["EESSI_TEST_GPU_HANG"] = "30"
        path = _cache_path(env)
        started = time.monotonic()
        proc = harness.run_guard(env, ["--timeout", "1"])
        elapsed = time.monotonic() - started
        try:
            changes, printed = _assert_prolog(proc)
            self.assertLess(elapsed, _stop_limit(1))
            self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->timeout"))
            self.assertTrue(printed)
            self.assertFalse(os.path.isfile(path))
            pid = int(Path(env["EESSI_TEST_HANG_PID"]).read_text(encoding="ascii"))
            _wait_until_dead(pid)
            self.assertFalse(_pid_alive(pid))
        finally:
            pid_path = Path(env["EESSI_TEST_HANG_PID"])
            if pid_path.is_file():
                try:
                    os.kill(int(pid_path.read_text(encoding="ascii")), signal.SIGKILL)
                except OSError:
                    pass

    def test_hung_reload_is_stopped(self):
        env = self._cpu_env(ZEN4, "zen2")
        env["TMPDIR"] = self.site.root
        env["SLURM_JOB_ID"] = "6161"
        env["EESSI_TEST_LMOD_HANG"] = "30"
        env["EESSI_TEST_HANG_PID"] = os.path.join(self.site.root, "hang.pid")
        started = time.monotonic()
        proc = harness.run_guard(env, ["--timeout", "1"])
        elapsed = time.monotonic() - started
        try:
            changes, _printed = _assert_prolog(proc)
            self.assertLess(elapsed, _stop_limit(1))
            self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->timeout"))
            self.assertNotIn("EESSI_SOFTWARE_SUBDIR", changes)
            pid = int(Path(env["EESSI_TEST_HANG_PID"]).read_text(encoding="ascii"))
            _wait_until_dead(pid)
            self.assertFalse(_pid_alive(pid))
        finally:
            pid_path = Path(env["EESSI_TEST_HANG_PID"])
            if pid_path.is_file():
                try:
                    os.kill(int(pid_path.read_text(encoding="ascii")), signal.SIGKILL)
                except OSError:
                    pass

    def test_lock_wait_ends_at_the_limit(self):
        env = self._cpu_env(ZEN4, "zen2")
        env["TMPDIR"] = self.site.root
        env["SLURM_JOB_ID"] = "6262"
        path = _cache_path(env)
        lock_path = path + ".lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            started = time.monotonic()
            proc = harness.run_guard(env, ["--timeout", "1"])
            elapsed = time.monotonic() - started
            changes, _printed = _assert_prolog(proc)
            self.assertLess(elapsed, _stop_limit(1))
            self.assertGreaterEqual(elapsed, 0.5)
            self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->timeout"))
            self.assertFalse(os.path.isfile(path))
            self.assertEqual(harness.log_lines(self.site.log), [])
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_sigterm_stops_the_decision(self):
        env = self._hang_env()
        env["EESSI_TEST_GPU_HANG"] = "30"
        proc = subprocess.Popen(
            [*harness.GUARD_ARGV, "--timeout", "30"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            env=env,
        )
        pid_path = Path(env["EESSI_TEST_HANG_PID"])
        try:
            for _ in range(50):
                if pid_path.is_file():
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(pid_path.is_file(), proc.stderr)
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, err)
            changes, _printed = apply_task_prolog(out)
            self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->terminated"))
            child = int(pid_path.read_text(encoding="ascii"))
            _wait_until_dead(child)
            self.assertFalse(_pid_alive(child))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            if pid_path.is_file():
                try:
                    os.kill(int(pid_path.read_text(encoding="ascii")), signal.SIGKILL)
                except OSError:
                    pass

    def test_limit_does_not_undo_a_cached_decision(self):
        env = self._hang_env(loaded=ZEN4, node="zen2")
        env["EESSI_TEST_GPU_HANG"] = "30"
        path = _cache_path(env)
        # The path query ran the archdetect stub; the run under test is the one to count.
        open(self.site.arch_log, "w").close()
        _write_cache(
            path,
            _Outcome(
                {"SENTINEL": "cached-decision", "EESSI_ARCH_GUARD_RELOADED": "from-cache"},
                "cached",
                "reloaded",
            ),
        )
        before = Path(path).read_bytes()
        started = time.monotonic()
        proc = harness.run_guard(env, ["--timeout", "1"])
        elapsed = time.monotonic() - started
        changes, printed = _assert_prolog(proc)
        self.assertLess(elapsed, 0.8)
        self.assertEqual(changes["SENTINEL"], "cached-decision")
        self.assertEqual(changes["EESSI_ARCH_GUARD_RELOADED"], "from-cache")
        self.assertEqual(printed, ["cached"])
        self.assertEqual(Path(path).read_bytes(), before)
        self.assertEqual(harness.log_lines(self.site.arch_log), [])

    def test_unopenable_cache_directory_at_the_deadline_is_the_error(self):
        # Before the decision a cache directory that does not open means no
        # cache, but the read at the deadline opens it outside any handler:
        # the OSError ends the run as a skip, not as a timeout.
        if os.geteuid() == 0:
            self.skipTest("root opens a directory of mode 000")
        env = self._hang_env()
        env["EESSI_TEST_GPU_HANG"] = "30"
        cache_dir = os.path.join(self.site.root, "cache")
        os.mkdir(cache_dir, 0)
        env["TMPDIR"] = cache_dir
        try:
            proc = harness.run_guard(env, ["--timeout", "1"])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
            self.assertEqual(
                proc.stdout,
                "print eessi-arch-guard: skipped, PermissionError: "
                "[Errno 13] Permission denied: %r\n" % cache_dir,
            )
            pid = int(Path(env["EESSI_TEST_HANG_PID"]).read_text(encoding="ascii"))
            _wait_until_dead(pid)
            self.assertFalse(_pid_alive(pid))
        finally:
            os.chmod(cache_dir, 0o700)
            pid_path = Path(env["EESSI_TEST_HANG_PID"])
            if pid_path.is_file():
                try:
                    os.kill(int(pid_path.read_text(encoding="ascii")), signal.SIGKILL)
                except OSError:
                    pass

    def test_chain_keeps_the_config_timeout(self):
        conf = harness.PROGRAM.with_name("eessi-arch-guard.conf")
        conf.write_text("timeout = 1\n", encoding="utf-8")
        subprocess.check_call(["sudo", "chown", "root:root", str(conf)])
        subprocess.check_call(["sudo", "chmod", "644", str(conf)])
        ok_path = os.path.join(self.site.root, "chain-ok.sh")
        with open(ok_path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho 'export FROM_CHAIN=yes'\n")
        os.chmod(ok_path, 0o755)
        env = self._hang_env()
        env["EESSI_TEST_GPU_HANG"] = "30"
        try:
            started = time.monotonic()
            proc = harness.run_guard(env, ["--chain", ok_path])
            elapsed = time.monotonic() - started
            changes, _printed = _assert_prolog(proc)
            self.assertLess(elapsed, _stop_limit(1))
            self.assertEqual(changes["FROM_CHAIN"], "yes")
            self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->timeout"))
        finally:
            subprocess.check_call(["sudo", "rm", "-f", str(conf)])
            pid_path = Path(env["EESSI_TEST_HANG_PID"])
            if pid_path.is_file():
                try:
                    os.kill(int(pid_path.read_text(encoding="ascii")), signal.SIGKILL)
                except OSError:
                    pass

    def test_setsid_grandchild_does_not_hold_the_guard(self):
        env = self._hang_env()
        env["EESSI_TEST_SETSID_HOLD"] = "30"
        started = time.monotonic()
        proc = harness.run_guard(env, ["--timeout", "1"])
        elapsed = time.monotonic() - started
        pid_path = Path(env["EESSI_TEST_HANG_PID"])
        try:
            changes, _printed = _assert_prolog(proc)
            self.assertLess(elapsed, _stop_limit(1))
            self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->timeout"))
        finally:
            if pid_path.is_file():
                try:
                    os.kill(int(pid_path.read_text(encoding="ascii")), signal.SIGKILL)
                except OSError:
                    pass

    def _killed_helper_run(self, foreign=None):
        """Kill the helper by timeout and list its private temporary directory.

        The directory belongs to this test alone, so the listing cannot see a
        temporary directory made by a test that runs at the same time. A
        ``foreign`` callable runs in a thread while the guard runs, standing
        for such a test.
        """
        private = os.path.join(self.site.root, "tmp")
        os.mkdir(private)
        env = self._cpu_env(ZEN4, "zen2")
        env["TMPDIR"] = private
        env["SLURM_JOB_ID"] = "6363"
        env["EESSI_TEST_APPLY_HANG"] = "30"
        before = set(Path(private).glob("eessi-arch-guard-*"))
        worker = threading.Thread(target=foreign) if foreign else None
        if worker:
            worker.start()
        started = time.monotonic()
        proc = harness.run_guard(env, ["--timeout", "1"])
        elapsed = time.monotonic() - started
        if worker:
            worker.join()
        changes, _printed = _assert_prolog(proc)
        self.assertLess(elapsed, _stop_limit(1))
        self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->timeout"))
        return before, set(Path(private).glob("eessi-arch-guard-*"))

    def test_killed_helper_leaves_no_tmp_directory(self):
        before, after = self._killed_helper_run()
        self.assertEqual(before, after)

    def test_tmp_leak_check_ignores_directories_of_other_tests(self):
        made = []

        def foreign():
            made.append(tempfile.mkdtemp(prefix="eessi-arch-guard-foreign-"))

        try:
            before, after = self._killed_helper_run(foreign)
            self.assertEqual(len(made), 1)
            self.assertEqual(before, after)
        finally:
            for path in made:
                shutil.rmtree(path, ignore_errors=True)

    def test_config_timeout_bounds_the_decision(self):
        conf = harness.PROGRAM.with_name("eessi-arch-guard.conf")
        conf.write_text("timeout = 1\n", encoding="utf-8")
        subprocess.check_call(["sudo", "chown", "root:root", str(conf)])
        subprocess.check_call(["sudo", "chmod", "644", str(conf)])
        env = self._hang_env()
        env["EESSI_TEST_GPU_HANG"] = "30"
        try:
            started = time.monotonic()
            proc = harness.run_guard(env)
            elapsed = time.monotonic() - started
            changes, _printed = _assert_prolog(proc)
            self.assertLess(elapsed, _stop_limit(1))
            self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->timeout"))
        finally:
            subprocess.check_call(["sudo", "rm", "-f", str(conf)])
            pid_path = Path(env["EESSI_TEST_HANG_PID"])
            if pid_path.is_file():
                try:
                    os.kill(int(pid_path.read_text(encoding="ascii")), signal.SIGKILL)
                except OSError:
                    pass

    def test_timeout_past_what_poll_can_wait_is_refused(self):
        # A wait takes at most 2**31 - 1 ms. A longer timeout is refused and
        # the decision runs under the default.
        env = self._cpu_env(ZEN4, "zen2")
        proc = harness.run_guard(env, ["--timeout", "2147483.647"])
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2, proc.stdout)
        self.assertEqual(proc.stderr, "")
        warning = "eessi-arch-guard: %s is not a positive number of seconds at most 2147483.647\n"
        for value in ("2147483.648", "9223372036"):
            proc = harness.run_guard(env, ["--timeout", value])
            changes, _printed = _assert_prolog(proc)
            self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2, (value, proc.stdout))
            self.assertEqual(proc.stderr, warning % "--timeout")
        conf = harness.PROGRAM.with_name("eessi-arch-guard.conf")
        conf.write_text("timeout = 9223372036\n", encoding="utf-8")
        subprocess.check_call(["sudo", "chown", "root:root", str(conf)])
        subprocess.check_call(["sudo", "chmod", "644", str(conf)])
        try:
            proc = harness.run_guard(env)
            changes, _printed = _assert_prolog(proc)
            self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2, proc.stdout)
            self.assertEqual(proc.stderr, warning % "config timeout")
        finally:
            subprocess.check_call(["sudo", "rm", "-f", str(conf)])

    def test_intel_a100_uses_the_directory_that_exists(self):
        # Do not bake in either Intel subdirectory. The first
        # candidate with a software directory wins, including when the
        # listed-first tree is absent and the GPU is an A100 (cc 8.0).
        icelake = "x86_64/intel/icelake"
        skylake = "x86_64/intel/skylake_avx512"
        source = _program_bytes()
        self.assertNotIn(icelake.encode(), source)
        self.assertNotIn(skylake.encode(), source)
        self.site.add_cpu(skylake)
        self.site.add_module(skylake, "R/4.4.2")
        self.site.write_cpuinfo(matches="%s:%s" % (icelake, skylake))
        env = harness.base_env(self.site, icelake, ["R/4.4.2"], gpu="1", gpu_cc="8.0")
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], skylake)
        self.assertEqual(changes["EESSI_ACCEL_SUBDIR"], "accel/nvidia/cc80")
        self.assertIn(icelake, changes["EESSI_ARCH_GUARD_RELOADED"])
        self.assertIn(skylake, changes["EESSI_ARCH_GUARD_RELOADED"])
        self.assertIn("accel/nvidia/cc80", changes["EESSI_ARCH_GUARD_RELOADED"])

    def _copy_cpuinfo(self, name):
        source = Path(__file__).resolve().parent / "fixtures" / "cpuinfo" / name
        shutil.copy(source, self.site.cpuinfo)

    def _stub_cpupath(self):
        env = os.environ.copy()
        env["EESSI_PROC_CPUINFO"] = self.site.cpuinfo
        script = os.path.join(self.site.init, "eessi_archdetect.sh")
        proc = subprocess.run(
            ["bash", script, "-a", "cpupath"],
            capture_output=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_kvm_sapphirerapids_without_waitpkg_falls_back(self):
        # The stub still offers sapphirerapids: its flag list matches
        # archdetect and does not require waitpkg. The guard must not.
        spr = "x86_64/intel/sapphirerapids"
        icelake = "x86_64/intel/icelake"
        self.site.add_cpu(spr)
        self.site.add_cpu(icelake)
        self.site.add_module(spr, "R/4.4.2")
        self.site.add_module(icelake, "R/4.4.2")
        self._copy_cpuinfo("sapphirerapids-kvm-no-waitpkg.cpuinfo")
        offered = self._stub_cpupath()
        self.assertTrue(offered.startswith(spr + ":"), offered)
        self.assertIn(icelake, offered)
        env = harness.base_env(self.site, spr, ["R/4.4.2"], gpu="none")
        proc = harness.run_guard(env)
        changes, printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], icelake)
        self.assertNotEqual(changes["EESSI_SOFTWARE_SUBDIR"], spr)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR_OVERRIDE"], icelake)
        self.assertEqual(changes["EESSI_ARCHDETECT_OPTIONS"], icelake)
        self.assertIn(spr, changes["EESSI_ARCH_GUARD_RELOADED"])
        self.assertIn(icelake, changes["EESSI_ARCH_GUARD_RELOADED"])
        self.assertTrue(printed)
        source = _program_bytes()
        self.assertNotIn(icelake.encode(), source)

    def test_pinned_reload_drops_the_loaded_module_paths(self):
        # The EESSI module unloads the tree it computes from
        # EESSI_ARCHDETECT_OPTIONS, so the purge has to see the task's value
        # and only the loads after it see the pin.
        spr = "x86_64/intel/sapphirerapids"
        icelake = "x86_64/intel/icelake"
        for cpu in (spr, icelake):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self._copy_cpuinfo("sapphirerapids-kvm-no-waitpkg.cpuinfo")
        env = harness.base_env(self.site, ZEN2, ["R/4.4.2"], gpu="none")
        env["EESSI_ARCHDETECT_OPTIONS"] = ZEN2
        env["EESSI_TEST_MODULE_PATHS"] = "1"
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], icelake)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR_OVERRIDE"], icelake)
        self.assertEqual(changes["EESSI_ARCHDETECT_OPTIONS"], icelake)
        self.assertEqual(
            changes["MODULEPATH"],
            os.path.join(self.site.prefix, "software", "linux", icelake, "modules", "all"),
        )

    def test_restore_helper_rejects_an_unsafe_pin(self):
        env = harness.base_env(self.site, ZEN2, ["R/4.4.2"])
        request = {
            "v": 1,
            "env": env,
            "eessi": "EESSI/2025.06",
            "modules": ["R/4.4.2"],
            "lmod": env["LMOD_CMD"],
            "pin": "../" + ZEN2,
        }
        proc = subprocess.run(
            [*harness.GUARD_ARGV, "--restore"],
            input=json.dumps(request),
            capture_output=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reply = json.loads(proc.stdout)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "restore request is malformed")
        self.assertEqual(harness.log_lines(self.site.log), [])

    def test_restore_helper_refuses_env_values_that_are_not_strings(self):
        # The guard's own requests hold only strings: another value in env
        # makes the request malformed, and NaN and Infinity, which are not
        # JSON, stop the helper.
        env = harness.base_env(self.site, ZEN2, ["R/4.4.2"])
        request = json.dumps(
            {
                "v": 1,
                "env": {**env, "EXTRA": "@VALUE@"},
                "eessi": "EESSI/2025.06",
                "modules": ["R/4.4.2"],
                "lmod": env["LMOD_CMD"],
            }
        )

        def restore(value):
            return subprocess.run(
                [*harness.GUARD_ARGV, "--restore"],
                input=request.replace('"@VALUE@"', value),
                capture_output=True,
                encoding="utf-8",
                env=env,
                check=False,
            )

        malformed = {"ok": False, "missing": [], "error": "restore request is malformed"}
        for value in ("null", "true", "false", "1", "1E5", "[]", '["x"]', "{}", '{"x": "y"}'):
            proc = restore(value)
            self.assertEqual(proc.returncode, 0, (value, proc.stderr))
            self.assertEqual(json.loads(proc.stdout), malformed, value)
        for value in ("NaN", "Infinity", "-Infinity"):
            proc = restore(value)
            self.assertEqual(proc.returncode, 1, (value, proc.stdout))
            self.assertEqual(proc.stdout, "", value)
            self.assertRegex(proc.stderr, r"\Aeessi-arch-guard: restore failed: .* is not JSON\n\Z")
        self.assertEqual(harness.log_lines(self.site.log), [])

    def test_kvm_sapphirerapids_falls_back_to_skylake_when_that_tree_exists(self):
        spr = "x86_64/intel/sapphirerapids"
        skylake = "x86_64/intel/skylake_avx512"
        self.site.add_cpu(spr)
        self.site.add_cpu(skylake)
        self.site.add_module(skylake, "R/4.4.2")
        self._copy_cpuinfo("sapphirerapids-kvm-no-waitpkg.cpuinfo")
        env = harness.base_env(self.site, spr, ["R/4.4.2"], gpu="none")
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], skylake)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR_OVERRIDE"], skylake)
        source = _program_bytes()
        self.assertNotIn(skylake.encode(), source)

    def test_real_sapphirerapids_keeps_the_target(self):
        spr = "x86_64/intel/sapphirerapids"
        self.site.add_cpu(spr)
        self.site.add_module(spr, "R/4.4.2")
        self._copy_cpuinfo("sapphirerapids-8488C.cpuinfo")
        offered = self._stub_cpupath()
        self.assertTrue(offered.startswith(spr + ":"), offered)
        self.assertIn("x86_64/intel/icelake:", offered)
        env = harness.base_env(self.site, spr, ["R/4.4.2"], gpu="none")
        proc = harness.run_guard(env)
        _assert_prolog(proc)
        self.assertEqual(proc.stdout, "")

    def test_emeraldrapids_without_waitpkg_falls_back(self):
        # GCC's emeraldrapids march is the Sapphire Rapids mask, including
        # waitpkg. A candidate with that leaf has to be dropped when the
        # shared flags omit it, the same way sapphirerapids is dropped.
        emr = "x86_64/intel/emeraldrapids"
        icelake = "x86_64/intel/icelake"
        self.site.add_cpu(emr)
        self.site.add_cpu(icelake)
        self.site.add_module(emr, "R/4.4.2")
        self.site.add_module(icelake, "R/4.4.2")
        self._copy_cpuinfo("sapphirerapids-kvm-no-waitpkg.cpuinfo")
        script = Path(self.site.init) / "eessi_archdetect.sh"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$2" = cpupath ]; then\n'
            "  printf '%%s\\n' '%s:%s'\n"
            "fi\n" % (emr, icelake),
            encoding="utf-8",
        )
        script.chmod(0o755)
        env = harness.base_env(self.site, emr, ["R/4.4.2"], gpu="none")
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes.get("EESSI_SOFTWARE_SUBDIR"), icelake, proc.stdout)
        self.assertEqual(changes.get("EESSI_SOFTWARE_SUBDIR_OVERRIDE"), icelake, proc.stdout)

    def test_graniterapids_without_waitpkg_falls_back(self):
        spr = "x86_64/intel/sapphirerapids"
        gnr = "x86_64/intel/graniterapids"
        icelake = "x86_64/intel/icelake"
        text = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "cpuinfo"
            / "sapphirerapids-kvm-no-waitpkg.cpuinfo"
        ).read_text(encoding="ascii")
        text = text.replace("model           : 143", "model           : 173", 1)
        with open(self.site.cpuinfo, "w", encoding="ascii") as handle:
            handle.write(text)
        self.site.add_cpu(gnr)
        self.site.add_cpu(spr)
        self.site.add_cpu(icelake)
        self.site.add_module(icelake, "R/4.4.2")
        offered = self._stub_cpupath()
        self.assertTrue(offered.startswith(gnr + ":" + spr + ":"), offered)
        env = harness.base_env(self.site, gnr, ["R/4.4.2"], gpu="none")
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], icelake)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR_OVERRIDE"], icelake)

    def test_duplicated_flags_lines_across_identical_blocks_keep_all_flags(self):
        spr = "x86_64/intel/sapphirerapids"
        icelake = "x86_64/intel/icelake"
        self.site.add_cpu(spr)
        self.site.add_cpu(icelake)
        self.site.add_module(spr, "R/4.4.2")
        self.site.add_module(icelake, "R/4.4.2")
        cpuinfo = "\n".join(
            (
                "processor : 0",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 143",
                "flags : avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl "
                "avx512_vnni avx512_vbmi2 avx512_bf16 amx_tile waitpkg",
                "flags : avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl "
                "avx512_vnni avx512_vbmi2",
                "",
                "processor : 1",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 143",
                "flags : avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl "
                "avx512_vnni avx512_vbmi2 avx512_bf16 amx_tile waitpkg",
                "flags : avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl "
                "avx512_vnni avx512_vbmi2",
                "",
            )
        )
        with open(self.site.cpuinfo, "w", encoding="ascii") as handle:
            handle.write(cpuinfo)
        env = harness.base_env(self.site, spr, ["R/4.4.2"], gpu="none")
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], icelake)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR_OVERRIDE"], icelake)

    def test_scoped_processor_intersects_repeated_flags_lines(self):
        # Processor `target` lists a Sapphire Rapids flags line and then an
        # Ice Lake line. A second processor is outside the bind list, so the
        # file is rewritten. The rewritten record must keep the flags both
        # lines share. The first line alone still names Sapphire Rapids.
        spr = "x86_64/intel/sapphirerapids"
        icelake = "x86_64/intel/icelake"
        sched = os.sched_getaffinity(0)
        if not sched:
            self.skipTest("sched_getaffinity returned no CPUs")
        pool = set(sched)
        cgroup = _cgroup_cpus()
        if cgroup is not None:
            pool &= set(cgroup)
        if not pool:
            self.skipTest("no processor is in both the affinity mask and the cgroup cpuset")
        target = min(pool)
        other = 4095 if target != 4095 else 4094
        rich = (
            "avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl "
            "avx512_vnni avx512_vbmi2 avx512_bf16 amx_tile waitpkg"
        )
        shared = "avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl avx512_vnni avx512_vbmi2"
        self.site.add_cpu(spr)
        self.site.add_cpu(icelake)
        self.site.add_module(spr, "R/4.4.2")
        self.site.add_module(icelake, "R/4.4.2")
        cpuinfo = "\n".join(
            (
                "processor : %d" % target,
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 143",
                "flags : " + rich,
                "flags : " + shared,
                "",
                "processor : %d" % other,
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 143",
                "flags : " + rich,
                "",
            )
        )
        with open(self.site.cpuinfo, "w", encoding="ascii") as handle:
            handle.write(cpuinfo)
        env = harness.base_env(self.site, spr, ["R/4.4.2"], gpu="none")
        env["SLURM_CPU_BIND_LIST"] = str(target)
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes.get("EESSI_SOFTWARE_SUBDIR"), icelake, proc.stdout)

    def test_real_emeraldrapids_kvm_cpuinfo_and_offline_gaps_fall_back(self):
        spr = "x86_64/intel/sapphirerapids"
        icelake = "x86_64/intel/icelake"
        affinity = sorted(os.sched_getaffinity(0))
        self.assertTrue(affinity, "expected at least one CPU in the live affinity mask")
        self.site.add_cpu(spr)
        self.site.add_cpu(icelake)
        self.site.add_module(spr, "R/4.4.2")
        self.site.add_module(icelake, "R/4.4.2")
        self._copy_cpuinfo("emeraldrapids-kvm-no-waitpkg.cpuinfo")
        env = harness.base_env(self.site, spr, ["R/4.4.2"], gpu="none")
        fixture_visible = [cpu for cpu in affinity if 0 <= cpu <= 7]
        if not fixture_visible:
            self.skipTest("fixture cpuinfo has processors 0-7, none are in the live affinity mask")
        first_pair = fixture_visible[:2]
        env["SLURM_CPU_BIND_LIST"] = (
            str(first_pair[0]) if len(first_pair) == 1 else ",".join(str(cpu) for cpu in first_pair)
        )
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], icelake)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR_OVERRIDE"], icelake)

        full_text = Path(self.site.cpuinfo).read_text(encoding="ascii")
        even_blocks = [
            block
            for block in full_text.strip().split("\n\n")
            if block.splitlines()[0].split(":")[-1].strip() in ("0", "2", "4", "6")
        ]
        Path(self.site.cpuinfo).write_text("\n\n".join(even_blocks) + "\n", encoding="ascii")
        env_gaps = harness.base_env(self.site, spr, ["R/4.4.2"], gpu="none")
        even_ids = [
            int(block.splitlines()[0].split(":")[-1].strip())
            for block in even_blocks
            if block.strip()
        ]
        gap_visible = [cpu for cpu in even_ids if cpu in affinity]
        if not gap_visible:
            self.skipTest("no even fixture processor is in the live affinity mask")
        env_gaps["SLURM_CPU_BIND_LIST"] = ",".join(str(cpu) for cpu in gap_visible[:2])
        proc_gaps = harness.run_guard(env_gaps)
        changes_gaps, _printed = _assert_prolog(proc_gaps)
        self.assertEqual(changes_gaps["EESSI_SOFTWARE_SUBDIR"], icelake)
        self.assertEqual(changes_gaps["EESSI_SOFTWARE_SUBDIR_OVERRIDE"], icelake)

    def test_slurm_cpu_bind_hostlist_uses_that_processor(self):
        # Processor 0 is Haswell. Processor 1 is Sapphire Rapids. The stub,
        # like an unscoped read, reports the first processor. A task whose
        # Slurm bind list is the hostlist cpu[1] must be judged on processor 1.
        spr = "x86_64/intel/sapphirerapids"
        haswell = "x86_64/intel/haswell"
        self.site.add_cpu(spr)
        self.site.add_cpu(haswell)
        self.site.add_module(spr, "R/4.4.2")
        self.site.add_module(haswell, "R/4.4.2")
        cpuinfo = "\n".join(
            (
                "processor : 0",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 60",
                "model name : Intel Haswell",
                "flags : avx2 fma",
                "",
                "processor : 1",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 143",
                "model name : Intel Sapphire Rapids",
                "flags : avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl "
                "avx512_bf16 amx_tile waitpkg",
                "",
            )
        )
        with open(self.site.cpuinfo, "w", encoding="ascii") as handle:
            handle.write(cpuinfo)
        env = harness.base_env(self.site, spr, ["R/4.4.2"], gpu="none")
        env["SLURM_CPU_BIND_LIST"] = "cpu[1]"
        env["SLURM_CPU_BIND_TYPE"] = "map_cpu"
        proc = harness.run_guard(env)
        # A matching target prints nothing. A reload to Haswell is the bug.
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", proc.stdout)

    def test_mask_cpu_list_keeps_the_masked_processor(self):
        # Processor 0 is Haswell. Processor 1 is Sapphire Rapids. Slurm
        # stores the whole --cpu-bind=mask_cpu:0x2,0x4 string in
        # SLURM_CPU_BIND_LIST: one hex mask per task, for CPU 1 and CPU 2.
        # CPU 0 is not in that list, so the loaded Sapphire Rapids stack
        # must stay put.
        spr = "x86_64/intel/sapphirerapids"
        haswell = "x86_64/intel/haswell"
        self.site.add_cpu(spr)
        self.site.add_cpu(haswell)
        self.site.add_module(spr, "R/4.4.2")
        self.site.add_module(haswell, "R/4.4.2")
        cpuinfo = "\n".join(
            (
                "processor : 0",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 60",
                "model name : Intel Haswell",
                "flags : avx2 fma",
                "",
                "processor : 1",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 143",
                "model name : Intel Sapphire Rapids",
                "flags : avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl "
                "avx512_bf16 amx_tile waitpkg",
                "",
            )
        )
        with open(self.site.cpuinfo, "w", encoding="ascii") as handle:
            handle.write(cpuinfo)
        env = harness.base_env(self.site, spr, ["R/4.4.2"], gpu="none")
        env["SLURM_CPU_BIND_LIST"] = "0x2,0x4"
        env["SLURM_CPU_BIND_TYPE"] = "mask_cpu"
        env["SLURM_CPU_BIND"] = "quiet,mask_cpu:0x2,0x4"
        proc = harness.run_guard(env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", proc.stdout)

    def test_slurm_cpu_bind_reloads_using_scoped_cpuinfo(self):
        # Processor 0 is Sapphire Rapids. Processor 1 is Haswell. A task
        # bound to cpu[1] must reload to Haswell without Lmod reading
        # processor 0 from the unscoped cpuinfo.
        spr = "x86_64/intel/sapphirerapids"
        haswell = "x86_64/intel/haswell"
        self.site.add_cpu(spr)
        self.site.add_cpu(haswell)
        self.site.add_cpu(ZEN4)
        self.site.add_module(spr, "R/4.4.2")
        self.site.add_module(haswell, "R/4.4.2")
        self.site.add_module(ZEN4, "R/4.4.2")
        cpuinfo = "\n".join(
            (
                "processor : 0",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 143",
                "model name : Intel Sapphire Rapids",
                "flags : avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl "
                "avx512_bf16 amx_tile waitpkg",
                "",
                "processor : 1",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 60",
                "model name : Intel Haswell",
                "flags : avx2 fma",
                "",
            )
        )
        with open(self.site.cpuinfo, "w", encoding="ascii") as handle:
            handle.write(cpuinfo)
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        env["SLURM_CPU_BIND_LIST"] = "cpu[1]"
        proc = harness.run_guard(env)
        changes, _printed = _assert_prolog(proc)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], haswell)
        self.assertNotIn("EESSI_PROC_CPUINFO", changes)
        self.assertNotIn("EESSI_ARCH_GUARD_FAILED", changes)

    def test_malformed_trailing_comma_cpu_bind_is_ignored(self):
        spr = "x86_64/intel/sapphirerapids"
        haswell = "x86_64/intel/haswell"
        self.site.add_cpu(spr)
        self.site.add_cpu(haswell)
        self.site.add_module(spr, "R/4.4.2")
        self.site.add_module(haswell, "R/4.4.2")
        cpuinfo = "\n".join(
            (
                "processor : 0",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 60",
                "model name : Intel Haswell",
                "flags : avx2 fma",
                "",
                "processor : 1",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 143",
                "model name : Intel Sapphire Rapids",
                "flags : avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl "
                "avx512_bf16 amx_tile waitpkg",
                "",
            )
        )
        with open(self.site.cpuinfo, "w", encoding="ascii") as handle:
            handle.write(cpuinfo)
        env = harness.base_env(self.site, haswell, ["R/4.4.2"], gpu="none")
        env["SLURM_CPU_BIND_LIST"] = "1,"
        proc = harness.run_guard(env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", proc.stdout)

    def test_malformed_space_separated_cpu_bind_is_ignored(self):
        spr = "x86_64/intel/sapphirerapids"
        haswell = "x86_64/intel/haswell"
        self.site.add_cpu(spr)
        self.site.add_cpu(haswell)
        self.site.add_module(spr, "R/4.4.2")
        self.site.add_module(haswell, "R/4.4.2")
        cpuinfo = "\n".join(
            (
                "processor : 0",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 60",
                "model name : Intel Haswell",
                "flags : avx2 fma",
                "",
                "processor : 1",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 143",
                "model name : Intel Sapphire Rapids",
                "flags : avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl "
                "avx512_bf16 amx_tile waitpkg",
                "",
            )
        )
        with open(self.site.cpuinfo, "w", encoding="ascii") as handle:
            handle.write(cpuinfo)
        env = harness.base_env(self.site, haswell, ["R/4.4.2"], gpu="none")
        env["SLURM_CPU_BIND_LIST"] = "0 1"
        proc = harness.run_guard(env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", proc.stdout)

    def test_scoped_cpuinfo_uses_tmpdir(self):
        spr = "x86_64/intel/sapphirerapids"
        self.site.add_cpu(spr)
        self.site.add_module(spr, "R/4.4.2")
        cpuinfo = "\n".join(
            (
                "processor : 0",
                "vendor_id : GenuineIntel",
                "flags : avx2 fma",
                "",
                "processor : 1",
                "vendor_id : GenuineIntel",
                "flags : avx2 fma avx512f waitpkg",
                "",
            )
        )
        with open(self.site.cpuinfo, "w", encoding="ascii") as handle:
            handle.write(cpuinfo)
        with tempfile.TemporaryDirectory() as tmpdir:
            seen_file = Path(tmpdir) / "seen.txt"
            script = Path(self.site.init) / "eessi_archdetect.sh"
            original = script.read_text(encoding="utf-8")
            script.write_text(
                f"#!/bin/bash\nprintf '%s\\n' \"$EESSI_PROC_CPUINFO\" > {seen_file}\n"
                + original.split("\n", 1)[1],
                encoding="utf-8",
            )
            env = harness.base_env(self.site, spr, ["R/4.4.2"], gpu="none")
            env["TMPDIR"] = tmpdir
            env["SLURM_CPU_BIND_LIST"] = "cpu[1]"
            proc = harness.run_guard(env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            seen = seen_file.read_text(encoding="utf-8").strip()
            self.assertEqual(Path(seen).parent, Path(tmpdir), seen)
            self.assertTrue(Path(seen).name.startswith("eessi-arch-guard-cpu-"), seen)
            self.assertFalse(Path(seen).exists())

    def test_prepare_cpuinfo_rejects_fifo_and_directory(self):
        script = Path(self.site.init) / "eessi_archdetect.sh"
        script.write_text(
            '#!/bin/sh\ncase "$*" in\n*cpupath*) echo x86_64/amd/zen4 ;;\n*) exit 2 ;;\nesac\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        fifo = Path(self.site.root) / "cpu.fifo"
        os.mkfifo(fifo, 0o600)
        directory = Path(self.site.root) / "cpu.dir"
        directory.mkdir()
        for path in (fifo, directory):
            with self.subTest(path=path.name):
                env = harness.base_env(self.site, ZEN4, [], gpu="none")
                env["EESSI_PROC_CPUINFO"] = str(path)
                env["SLURM_CPU_BIND_LIST"] = "0"
                proc = harness.run_with_timeout(
                    [*harness.GUARD_ARGV, "--dry-run", "--json"],
                    env=env,
                    timeout=1.5,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                doc = json.loads(proc.stdout)
                self.assertEqual(doc["decision"], "noop")

    def test_buffered_fifo_cpuinfo_is_rejected(self):
        cpus = sorted(os.sched_getaffinity(0))
        if len(cpus) < 2:
            self.skipTest("the buffered FIFO test needs two processors")
        first, selected = cpus[:2]
        cpuinfo = (
            f"processor : {first}\n"
            "vendor_id : AuthenticAMD\n"
            "model name : AMD EPYC 7702 64-Core Processor\n"
            "flags : fpu avx2 fma\n\n"
            f"processor : {selected}\n"
            "vendor_id : AuthenticAMD\n"
            "model name : AMD EPYC 9654 96-Core Processor\n"
            "flags : fpu avx2 fma vaes avx512f avx512ifma\n"
        ).encode("ascii")
        fifo = Path(self.site.root) / "buffered-cpuinfo.fifo"
        os.mkfifo(fifo, 0o600)
        keeper = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            try:
                self.assertEqual(os.write(writer, cpuinfo), len(cpuinfo))
            finally:
                os.close(writer)
            env = harness.base_env(self.site, ZEN4, [], gpu="none")
            env["EESSI_PROC_CPUINFO"] = str(fifo)
            env["SLURM_CPU_BIND_LIST"] = str(selected)
            proc = harness.run_with_timeout(
                [*harness.GUARD_ARGV, "--dry-run", "--json", "--timeout", "0.2"],
                env=env,
                timeout=3.0,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            doc = json.loads(proc.stdout)
            self.assertEqual(doc["decision"], "failed")
            self.assertTrue(doc["marker"].endswith("->timeout"), doc)
        finally:
            os.close(keeper)

    def test_oversized_processor_id_does_not_bypass_cpu_scoping(self):
        spr = "x86_64/intel/sapphirerapids"
        generic = "x86_64/generic"
        self.site.add_cpu(spr)
        self.site.add_cpu(generic)
        self.site.add_module(spr, "R/4.4.2")
        self.site.add_module(generic, "R/4.4.2")
        cpuinfo = "\n".join(
            (
                "processor : 999999999999999999999999",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 143",
                "flags : avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl "
                "avx512_vnni avx512_vbmi2 avx512_bf16 amx_tile waitpkg",
                "",
            )
        )
        with open(self.site.cpuinfo, "w", encoding="ascii") as handle:
            handle.write(cpuinfo)
        env = harness.base_env(self.site, spr, ["R/4.4.2"], gpu="none")
        env["SLURM_CPU_BIND_LIST"] = "0"
        proc = harness.run_guard(env, ["--dry-run", "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["decision"], "reloaded")
        self.assertEqual(doc["now"], generic)

    def test_cache_does_not_reuse_a_decision_for_another_cpu_bind(self):
        # The noop for cpu[1] must not be replayed for a task in the same
        # job whose bind list is cpu[0].
        spr = "x86_64/intel/sapphirerapids"
        haswell = "x86_64/intel/haswell"
        self.site.add_cpu(spr)
        self.site.add_cpu(haswell)
        self.site.add_module(spr, "R/4.4.2")
        self.site.add_module(haswell, "R/4.4.2")
        cpuinfo = "\n".join(
            (
                "processor : 0",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 60",
                "flags : avx2 fma",
                "",
                "processor : 1",
                "vendor_id : GenuineIntel",
                "cpu family : 6",
                "model : 143",
                "flags : avx2 fma avx512f avx512bw avx512cd avx512dq avx512vl "
                "avx512_bf16 amx_tile waitpkg",
                "",
            )
        )
        with open(self.site.cpuinfo, "w", encoding="ascii") as handle:
            handle.write(cpuinfo)
        env = harness.base_env(self.site, spr, ["R/4.4.2"], gpu="none")
        env["SLURM_JOB_ID"] = "bind-cache"
        env["SLURM_STEP_ID"] = "0"
        env["TMPDIR"] = self.site.root
        env["SLURM_CPU_BIND_LIST"] = "cpu[1]"
        first = harness.run_guard(env)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, "", first.stdout)
        env["SLURM_CPU_BIND_LIST"] = "cpu[0]"
        second = harness.run_guard(env)
        self.assertIn(haswell, second.stdout, second.stdout)
        changes, _printed = _assert_prolog(second)
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], haswell)

    def test_help_and_version(self):
        proc = harness.run_guard({"PATH": "/usr/bin:/bin"}, ["--version"])
        # The version is followed by the build (COMPAT.md).
        self.assertRegex(
            proc.stdout,
            r"\A" + re.escape(_program_version()) + r" \([^()\n]+, [^()\n]+\)\n\Z",
        )
        proc = harness.run_guard({"PATH": "/usr/bin:/bin"}, ["--help"])
        self.assertIn("--shell", proc.stdout)
        self.assertIn("--dry-run", proc.stdout)
        self.assertIn("--json", proc.stdout)
        self.assertIn("--check-submit", proc.stdout)
        self.assertIn("--partition", proc.stdout)
        self.assertIn("--restore", proc.stdout)
        self.assertIn("--version", proc.stdout)
        self.assertIn("--chain", proc.stdout)
        self.assertIn("--timeout", proc.stdout)
        self.assertIn("-h", proc.stdout)
        self.assertEqual(proc.returncode, 0)

    def test_dry_run_writes_no_files_and_does_not_run_chain_or_lmod(self):
        import socket

        scratch = tempfile.mkdtemp(prefix="eag-dryrun-")
        os.chmod(scratch, 0o700)
        sockdir = tempfile.mkdtemp(prefix="eag-dryrun-sock-")
        sock_path = os.path.join(sockdir, "syslog.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(sock_path)
        sock.settimeout(0.1)
        chain_sentinel = os.path.join(self.site.root, "chain-ran")
        chain_script = os.path.join(self.site.root, "chain.sh")
        with open(chain_script, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\ntouch '%s'\necho 'export FROM_CHAIN=1'\n" % chain_sentinel)
        os.chmod(chain_script, 0o755)
        try:
            env = self._cpu_env(ZEN4, "zen2", modules=("R/4.4.2",))
            env["TMPDIR"] = scratch
            env["EESSI_ARCH_GUARD_WORKDIR"] = scratch
            env["SLURM_JOB_ID"] = "9001"
            env["SLURM_STEP_ID"] = "0"
            env["SLURM_LOCALID"] = "0"

            before_scratch = sorted(os.listdir(scratch))
            self.assertEqual(before_scratch, [])

            for extra in (
                ["--dry-run", "--chain", chain_script, "--syslog", f"unix://{sock_path}"],
                ["--dry-run", "--json", "--strict", "--chain", chain_script],
                ["--dry-run", "--shell"],
            ):
                proc = harness.run_guard(env, extra)
                self.assertEqual(proc.returncode, 0, (extra, proc.stderr, proc.stdout))
                self.assertEqual(proc.stderr, "", extra)
                for forbidden in ("export ", "unset ", "print "):
                    self.assertNotIn(forbidden, proc.stdout, (extra, proc.stdout))
                self.assertEqual(sorted(os.listdir(scratch)), before_scratch, extra)
                self.assertFalse(os.path.exists(chain_sentinel), extra)
                self.assertFalse(os.path.exists(self.site.log), extra)
                self.assertEqual(harness.log_lines(self.site.log), [], extra)

            with self.assertRaises(socket.timeout):
                sock.recv(4096)

            text_proc = harness.run_guard(
                env, ["--dry-run", "--chain", chain_script, "--syslog", f"unix://{sock_path}"]
            )
            self.assertIn("mode: prolog\n", text_proc.stdout)
            self.assertIn(f"chain: {chain_script} (cli, valid)\n", text_proc.stdout)
            self.assertIn(f"syslog: unix://{sock_path}\n", text_proc.stdout)
            self.assertIn("(miss)\n", text_proc.stdout)
            self.assertIn("decision: reloaded\n", text_proc.stdout)
            self.assertIn(f"was: {ZEN4}\n", text_proc.stdout)
            self.assertIn(f"now: {ZEN2}\n", text_proc.stdout)
            self.assertIn("eessi: EESSI/2025.06\n", text_proc.stdout)
            self.assertIn("modules: R/4.4.2\n", text_proc.stdout)
            self.assertIn(f"marker: {ZEN4}->{ZEN2}\n", text_proc.stdout)
            self.assertIn("exit: 0\n", text_proc.stdout)

            json_proc = harness.run_guard(env, ["--dry-run", "--json", "--strict"])
            payload = json.loads(json_proc.stdout)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["kind"], "decision")
            self.assertEqual(payload["decision"], "reloaded")
            self.assertTrue(payload["reload_needed"])
            self.assertEqual(payload["exit_semantic"], "ok")
            self.assertTrue(payload["strict"])
            self.assertEqual(payload["was"], ZEN4)
            self.assertEqual(payload["now"], ZEN2)
            self.assertEqual(payload["modules"], ["R/4.4.2"])
            self.assertFalse(payload["cache"]["hit"])
            self.assertEqual(payload["exit"], 0)
            self.assertEqual(sorted(os.listdir(scratch)), before_scratch)
        finally:
            sock.close()
            shutil.rmtree(sockdir, ignore_errors=True)
            shutil.rmtree(scratch, ignore_errors=True)

    def test_dry_run_readonly_tmpdir_and_decisions(self):
        scratch = tempfile.mkdtemp(prefix="eag-dryrun-ro-")
        os.chmod(scratch, 0o500)
        try:
            skip_proc = harness.run_guard(
                {"PATH": "/usr/bin:/bin", "TMPDIR": scratch}, ["--dry-run"]
            )
            self.assertEqual(skip_proc.returncode, 0, skip_proc.stderr)
            self.assertIn("decision: skip\n", skip_proc.stdout)
            self.assertIn("detail: EESSI is not loaded\n", skip_proc.stdout)

            noop_env = self._cpu_env(ZEN2, "zen2")
            noop_env["TMPDIR"] = scratch
            noop_env["SLURM_JOB_ID"] = "707"
            noop_proc = harness.run_guard(noop_env, ["--dry-run", "--json"])
            self.assertEqual(noop_proc.returncode, 0, noop_proc.stderr)
            noop_data = json.loads(noop_proc.stdout)
            self.assertEqual(noop_data["decision"], "noop")
            self.assertEqual(noop_data["was"], ZEN2)
            self.assertEqual(noop_data["now"], ZEN2)
            self.assertFalse(noop_data["cache"]["hit"])

            reload_env = self._cpu_env(ZEN4, "zen2")
            reload_env["TMPDIR"] = scratch
            reload_env["SLURM_JOB_ID"] = "708"
            reload_proc = harness.run_guard(reload_env, ["--dry-run"])
            self.assertEqual(reload_proc.returncode, 0, reload_proc.stderr)
            self.assertIn("decision: reloaded\n", reload_proc.stdout)

            strict_env = dict(reload_env)
            strict_env.pop("TMPDIR", None)
            strict_fail = harness.run_guard(strict_env, ["--dry-run", "--strict", "--json"])
            self.assertEqual(strict_fail.returncode, STRICT_FAILURE_EXIT)
            strict_data = json.loads(strict_fail.stdout)
            self.assertEqual(strict_data["decision"], "failed")
            self.assertEqual(strict_data["exit"], STRICT_FAILURE_EXIT)
            self.assertTrue(strict_data["marker"].endswith("->restore-failed"))
            self.assertEqual(sorted(os.listdir(scratch)), [])
            self.assertEqual(harness.log_lines(self.site.log), [])
        finally:
            os.chmod(scratch, 0o700)
            shutil.rmtree(scratch, ignore_errors=True)

    def test_dry_run_reports_config_and_untrusted_config(self):
        conf = harness.PROGRAM.with_name("eessi-arch-guard.conf")
        chain_script = os.path.join(self.site.root, "site-chain.sh")
        chain_sentinel = os.path.join(self.site.root, "site-chain-ran")
        with open(chain_script, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\ntouch '%s'\n" % chain_sentinel)
        os.chmod(chain_script, 0o755)
        conf.write_text(f"chain = {chain_script}\ntimeout = 15\n", encoding="utf-8")
        try:
            # Untrusted (not owned by root)
            env = self._cpu_env(ZEN2, "zen2")
            bad_proc = harness.run_guard(env, ["--dry-run", "--json"])
            self.assertEqual(bad_proc.returncode, 0)
            self.assertIn("config ignored:", bad_proc.stderr)
            bad_data = json.loads(bad_proc.stdout)
            self.assertEqual(bad_data["config"]["status"], "ignored")
            self.assertEqual(bad_data["config"]["path"], str(conf))
            self.assertIn("not owned by root", bad_data["config"]["warning"])
            self.assertEqual(bad_data["chain"]["status"], "none")
            self.assertEqual(bad_data["timeout"], 60.0)

            # Root-owned valid config
            subprocess.check_call(["sudo", "chown", "root:root", str(conf)])
            subprocess.check_call(["sudo", "chmod", "644", str(conf)])
            ok_proc = harness.run_guard(env, ["--dry-run", "--json"])
            self.assertEqual(ok_proc.returncode, 0)
            self.assertEqual(ok_proc.stderr, "")
            ok_data = json.loads(ok_proc.stdout)
            self.assertEqual(ok_data["config"]["status"], "loaded")
            self.assertEqual(ok_data["chain"]["path"], chain_script)
            self.assertEqual(ok_data["chain"]["source"], "config")
            self.assertEqual(ok_data["chain"]["status"], "valid")
            self.assertEqual(ok_data["timeout"], 15.0)
            self.assertFalse(os.path.exists(chain_sentinel))
        finally:
            subprocess.check_call(["sudo", "rm", "-f", str(conf)])

    def test_dry_run_reads_existing_cache_without_creating_lock(self):
        scratch = tempfile.mkdtemp(prefix="eag-dryrun-cache-")
        os.chmod(scratch, 0o700)
        try:
            env = self._cpu_env(ZEN4, "zen2")
            env["TMPDIR"] = scratch
            env["SLURM_JOB_ID"] = "5150"
            env["SLURM_STEP_ID"] = "0"
            cache_path = _cache_path(env)
            self.assertIsNotNone(cache_path)
            _write_cache(
                cache_path,
                _Outcome(
                    {"EESSI_ARCH_GUARD_RELOADED": f"{ZEN4}->{ZEN2}"},
                    "cached reload",
                    "reloaded",
                ),
            )
            before = sorted(os.listdir(scratch))
            self.assertEqual(len(before), 1)
            before_bytes = Path(cache_path).read_bytes()

            proc = harness.run_guard(env, ["--dry-run", "--json"])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            self.assertEqual(data["cache"]["path"], cache_path)
            self.assertEqual(os.path.dirname(cache_path), scratch)
            self.assertRegex(
                os.path.basename(cache_path),
                r"\A\.eessi-arch-guard\.%d\.[A-Za-z0-9._-]+\.5150\.0\.prolog\.[0-9a-f]{16}\Z"
                % os.getuid(),
            )
            self.assertTrue(data["cache"]["hit"])
            self.assertEqual(data["cache"]["cached_kind"], "reloaded")
            self.assertEqual(data["decision"], "reloaded")
            self.assertEqual(sorted(os.listdir(scratch)), before)
            self.assertEqual(Path(cache_path).read_bytes(), before_bytes)
            self.assertFalse(os.path.exists(cache_path + ".lock"))
            self.assertEqual(harness.log_lines(self.site.log), [])
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _golden_run(self, sheet, label, env, args, work):
        proc = harness.run_guard(env, args)
        text = golden.render(proc.returncode, proc.stdout, proc.stderr, work=work)
        sheet.check(self, label, _CACHE_NAME_RE.sub("@CACHE@", text))
        return proc

    def test_dry_run_output(self):
        sheet = golden.Sheet("integration-dry-run")
        self.addCleanup(sheet.finish)
        scratch = tempfile.mkdtemp(prefix="eag-dryrun-output-")
        os.chmod(scratch, 0o700)
        chain_script = os.path.join(self.site.root, "dry-run-chain.sh")
        with open(chain_script, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho 'export P=1'\n")
        os.chmod(chain_script, 0o755)
        work = [scratch, self.site.root]
        try:
            cases = [
                ("no site, text", {"PATH": "/usr/bin:/bin", "TMPDIR": scratch}, ["--dry-run"]),
                (
                    "no site, json",
                    {"PATH": "/usr/bin:/bin", "TMPDIR": scratch},
                    ["--dry-run", "--json"],
                ),
                (
                    "no site, decision json",
                    {"PATH": "/usr/bin:/bin", "TMPDIR": scratch},
                    ["--json"],
                ),
                (
                    "reload, strict, chain, text",
                    {
                        **self._cpu_env(ZEN4, "zen2", modules=("R/4.4.2",)),
                        "TMPDIR": scratch,
                        "EESSI_ARCH_GUARD_WORKDIR": scratch,
                        "SLURM_JOB_ID": "314",
                        "SLURM_STEP_ID": "2",
                        "SLURM_LOCALID": "0",
                    },
                    ["--dry-run", "--strict", "--chain", chain_script, "--timeout", "15"],
                ),
                (
                    "reload, strict, chain, json",
                    {
                        **self._cpu_env(ZEN4, "zen2", modules=("R/4.4.2",)),
                        "TMPDIR": scratch,
                        "EESSI_ARCH_GUARD_WORKDIR": scratch,
                        "SLURM_JOB_ID": "314",
                        "SLURM_STEP_ID": "2",
                        "SLURM_LOCALID": "0",
                    },
                    [
                        "--dry-run",
                        "--json",
                        "--strict",
                        "--chain",
                        chain_script,
                        "--timeout",
                        "1.5",
                    ],
                ),
            ]
            for label, env, args in cases:
                with self.subTest(label=label):
                    self._golden_run(sheet, label, env, args, work)
            self.assertEqual(os.listdir(scratch), [])
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_invalid_environment_values(self):
        sheet = golden.Sheet("integration-invalid-environment")
        self.addCleanup(sheet.finish)
        cases = [
            ("EESSI_SOFTWARE_SUBDIR_OVERRIDE", "   "),
            ("EESSI_SOFTWARE_SUBDIR_OVERRIDE", "x86_64/amd/zen4/"),
            ("EESSI_SOFTWARE_SUBDIR_OVERRIDE", "x86_64/amd/unknownarch"),
            ("EESSI_ACCEL_SUBDIR", "accel/nvidia/cc90/"),
            ("EESSI_ACCEL_SUBDIR", " accel/nvidia/cc90"),
        ]
        for key, value in cases:
            env = self._cpu_env(ZEN4, "zen4", modules=("R/4.4.2",))
            env[key] = value
            with self.subTest(key=key, value=value):
                proc = self._golden_run(sheet, "%s=%r" % (key, value), env, [], [self.site.root])
                self.assertIn("EESSI_ARCH_GUARD_FAILED", proc.stdout, (key, value))
                self.assertIn("invalid-environment", proc.stdout, (key, value))

    def test_usage_errors(self):
        sheet = golden.Sheet("integration-usage-errors")
        self.addCleanup(sheet.finish)
        cases = [
            ["--unknown-option"],
            ["--"],
            ["--", "--help"],
            ["--timeout"],
            ["--chain"],
            ["--check-submit", "--partition"],
            ["--check-submit", "--partition", "bad name"],
            ["--check-submit", "--partition", "cpu", "--constraint"],
        ]
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        for args in cases:
            with self.subTest(args=args):
                self._golden_run(sheet, " ".join(repr(a) for a in args), env, args, [])

    def test_dangling_config_symlink_is_ignored_in_prolog_and_dry_run(self):
        prog_dir = os.path.join(self.site.root, "dangling-cfg")
        os.makedirs(prog_dir)
        program = os.path.join(prog_dir, "eessi-arch-guard")
        shutil.copy(harness.PROGRAM, program)
        os.chmod(program, 0o755)
        conf = os.path.join(prog_dir, "eessi-arch-guard.conf")
        os.symlink("missing-target.conf", conf)

        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        prolog = subprocess.run(
            [program], capture_output=True, encoding="utf-8", env=env, check=False
        )
        self.assertEqual(prolog.returncode, 0)
        self.assertEqual(prolog.stdout, "")
        self.assertIn("is a symlink and is ignored", prolog.stderr)

        dry = subprocess.run(
            [program, "--dry-run", "--json"],
            capture_output=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        self.assertEqual(dry.returncode, 0)
        doc = json.loads(dry.stdout)
        self.assertEqual(doc["config"]["status"], "ignored")
        self.assertEqual(doc["config"]["path"], conf)
        self.assertIn("is a symlink and is ignored", doc["config"]["warning"])

    def test_dry_run_text_replaces_invalid_utf8_bytes(self):
        env = {
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "EESSI_SOFTWARE_SUBDIR": os.fsdecode(b"x86_64/\xff/zen4"),
            "SLURM_JOB_ID": os.fsdecode(b"job\xff1"),
        }
        proc = harness.run_guard(env, ["--dry-run"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        lines = proc.stdout.splitlines()
        self.assertIn("job: job?1", lines)
        self.assertIn("was: x86_64/?/zen4", lines)
        self.assertIn("marker: x86_64/?/zen4->archdetect-failed", lines)

    def test_sigint_during_chain_takes_the_skip_path(self):
        ready = os.path.join(self.site.root, "chain-ready")
        chain = os.path.join(self.site.root, "slow-chain.sh")
        with open(chain, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\ntouch %r\nsleep 10\n" % ready)
        os.chmod(chain, 0o755)
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "EESSI_SOFTWARE_SUBDIR": ZEN4}

        for extra, expected_code, is_json in (
            ([], 0, False),
            (["--strict"], STRICT_FAILURE_EXIT, False),
            (["--json"], 0, True),
        ):
            with self.subTest(extra=extra):
                if os.path.exists(ready):
                    os.unlink(ready)
                proc = subprocess.Popen(
                    [*harness.GUARD_ARGV, "--chain", chain, *extra],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",
                )
                try:
                    deadline = time.monotonic() + 3.0
                    while not os.path.exists(ready) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(os.path.exists(ready))
                    proc.send_signal(signal.SIGINT)
                    stdout, stderr = proc.communicate(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    raise
                self.assertEqual(proc.returncode, expected_code, stderr)
                self.assertEqual(stderr, "")
                if is_json:
                    doc = json.loads(stdout)
                    self.assertEqual(doc["cause"], "KeyboardInterrupt")
                    self.assertEqual(doc["decision"], "failed")
                    self.assertEqual(doc["was"], ZEN4)
                    self.assertEqual(doc["exit"], expected_code)
                else:
                    self.assertEqual(
                        stdout, "print eessi-arch-guard: skipped, KeyboardInterrupt:\n"
                    )

    def test_check_submit_rejects_fifo_eb_stack_as_not_executable(self):
        fifo = os.path.join(self.site.root, "eb-stack.fifo")
        os.mkfifo(fifo, 0o755)
        env = harness.base_env(self.site, ZEN4, ())
        proc = harness.run_with_timeout(
            [*harness.GUARD_ARGV, "--check-submit", "--target", ZEN4, "--eb-stack", fifo, "--json"],
            env=env,
            timeout=2.0,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("eb-stack is not executable", proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["kind"], "check-submit")
        self.assertEqual(doc["exit"], 2)
        self.assertIn("eb-stack is not executable", doc["message"])
