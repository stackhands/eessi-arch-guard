"""What the guard decides for a loaded environment, from the outside.

Each case runs the program on a fake EESSI tree (tests/harness.py) with the
archdetect stub and the fake Lmod, and reads the exit status, the prolog or
shell text and the ``--dry-run --json`` report.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
from slurm_model import apply_task_prolog  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"
CC90 = "accel/nvidia/cc90"
CC120 = "accel/nvidia/cc120"


def make_site():
    site = harness.Site()
    for cpu in (ZEN2, ZEN4):
        site.add_cpu(cpu)
        site.add_module(cpu, "R/4.4.2")
    return site


def prolog(proc, code=0):
    """The changes and printed lines of a prolog run that exited with ``code``."""
    assert proc.returncode == code, (proc.returncode, proc.stdout, proc.stderr)
    for line in proc.stdout.split("\n"):
        if line:
            assert line.startswith(("export ", "unset ", "print ")), line
    return apply_task_prolog(proc.stdout)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class PolicyCase(unittest.TestCase):
    def setUp(self):
        self.site = make_site()
        self.addCleanup(self.site.close)

    def env(self, loaded=ZEN4, node="zen2", modules=("R/4.4.2",), **extra):
        self.site.write_cpuinfo(node)
        env = harness.base_env(self.site, loaded, list(modules), gpu="none")
        env.update(extra)
        return env

    def dry(self, env, args=()):
        proc = harness.run_guard(env, ["--dry-run", "--json", *args])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)


class SkipTests(PolicyCase):
    def test_not_loaded_is_silent(self):
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        proc = harness.run_guard(env)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        report = self.dry(env)
        self.assertEqual(report["decision"], "skip")
        self.assertEqual(report["detail"], "EESSI is not loaded")

    def test_opt_out_and_overrides_are_silent(self):
        for extra in (
            {"EESSI_ARCH_GUARD": "0"},
            {"EESSI_SOFTWARE_SUBDIR_OVERRIDE": ZEN2},
            {"EESSI_ARCHDETECT_OPTIONS_OVERRIDE": ZEN2},
        ):
            with self.subTest(extra=extra):
                env = self.env(**extra)
                proc = harness.run_guard(env)
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
                self.assertEqual(self.dry(env)["decision"], "skip")
                self.assertEqual(harness.log_lines(self.site.log), [])

    def test_unsafe_software_override_does_not_disable_guard(self):
        env = self.env(EESSI_SOFTWARE_SUBDIR_OVERRIDE="../../oops")
        changes, _printed = prolog(harness.run_guard(env))
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2)
        self.assertIn("EESSI_ARCH_GUARD_RELOADED", changes)
        self.assertEqual(self.dry(env)["decision"], "reloaded")

    def test_missing_software_directory_is_loud_and_configurable(self):
        bare = harness.Site()
        self.addCleanup(bare.close)
        bare.write_cpuinfo("zen2")
        env = harness.base_env(bare, ZEN4, [], gpu="none")
        changes, printed = prolog(harness.run_guard(env))
        self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->no-software-dir"))
        self.assertIn("no EESSI software directory", printed[0])
        env["EESSI_ARCH_GUARD_MISSING"] = "ignore"
        quiet = harness.run_guard(env)
        self.assertEqual((quiet.returncode, quiet.stdout), (0, ""))
        self.assertEqual(self.dry(env)["decision"], "skip")

    def test_first_candidate_with_a_software_directory_is_the_target(self):
        site = harness.Site()
        self.addCleanup(site.close)
        site.add_cpu(ZEN2)
        site.add_module(ZEN2, "R/4.4.2")
        site.write_cpuinfo(matches="x86_64/amd/zen5:%s:%s" % (ZEN4, ZEN2))
        env = harness.base_env(site, ZEN4, ["R/4.4.2"], gpu="none")
        changes, _printed = prolog(harness.run_guard(env))
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2)
        self.assertEqual(self.dry(env)["now"], ZEN2)


class InvalidEnvironmentTests(PolicyCase):
    def test_invalid_environment_values_are_loud(self):
        for extra in (
            {"EESSI_SOFTWARE_SUBDIR_OVERRIDE": "  "},
            {"EESSI_SOFTWARE_SUBDIR_OVERRIDE": ZEN4 + "/"},
            {"EESSI_ACCEL_SUBDIR": CC90 + "/"},
            {"EESSI_ACCEL_SUBDIR": " " + CC90},
        ):
            with self.subTest(extra=extra):
                env = self.env(**extra)
                changes, printed = prolog(harness.run_guard(env))
                self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->invalid-environment"))
                self.assertTrue(printed)
                self.assertIn("environment left as it was", printed[0])
                self.assertEqual(harness.log_lines(self.site.log), [])


class ReloadTests(PolicyCase):
    def test_same_target_does_not_restore(self):
        env = self.env(node="zen4")
        proc = harness.run_guard(env)
        prolog(proc)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(harness.log_lines(self.site.log), [])
        report = self.dry(env)
        self.assertEqual(report["decision"], "noop")
        self.assertEqual(report["now"], ZEN4)

    def test_cpu_mismatch_publishes_a_delta(self):
        env = self.env(SLURM_JOB_ID="4242", SLURM_STEP_ID="batch")
        changes, _printed = prolog(harness.run_guard(env))
        self.assertEqual(changes["EESSI_SOFTWARE_SUBDIR"], ZEN2)
        self.assertIn(ZEN2, changes["PATH"])
        self.assertEqual(changes["EESSI_ARCH_GUARD_RELOADED"], "%s->%s" % (ZEN4, ZEN2))
        self.assertNotIn("SLURM_JOB_ID", changes)
        self.assertNotIn("TMPDIR", changes)

    def test_dry_run_reports_the_reload(self):
        env = self.env(EESSI_ACCEL_SUBDIR="")
        report = self.dry(env)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["kind"], "decision")
        self.assertEqual(report["decision"], "reloaded")
        self.assertIs(report["reload_needed"], True)
        self.assertEqual(report["exit_semantic"], "ok")
        self.assertEqual(report["eessi"], "EESSI/2025.06")
        self.assertEqual(report["modules"], ["R/4.4.2"])
        self.assertEqual((report["was"], report["now"]), (ZEN4, ZEN2))
        self.assertEqual(report["marker"], "%s->%s" % (ZEN4, ZEN2))
        text = harness.run_guard(env, ["--dry-run"]).stdout
        self.assertIn("decision: reloaded\n", text)
        self.assertIn("modules: R/4.4.2\n", text)
        self.assertIn("timeout: 60\n", text)

    def test_dry_run_reports_a_failed_decision(self):
        env = self.env(LOADEDMODULES="EESSI/2025.06:bad\nmod/1")
        report = self.dry(env)
        self.assertEqual(report["decision"], "failed")
        self.assertTrue(report["marker"].endswith("->unsafe-module-name"))

    def test_year_module_keeps_the_loaded_order(self):
        env = self.env(LOADEDMODULES="EESSI/2026.06:2026:GCC/15.2.0")
        report = self.dry(env)
        self.assertEqual(report["eessi"], "EESSI/2026.06")
        self.assertEqual(report["modules"], ["2026", "GCC/15.2.0"])

    def test_unsafe_restore_value_publishes_nothing_else(self):
        env = self.env(EESSI_TEST_INJECT_NEWLINE="1")
        changes, _printed = prolog(harness.run_guard(env))
        self.assertEqual(set(changes), {"EESSI_ARCH_GUARD_FAILED"})
        self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->unsafe-environment"))

    def test_bad_module_name_does_not_restore(self):
        env = self.env(LOADEDMODULES="EESSI/2025.06:R/4.4.2\nexport LD_PRELOAD=evil")
        changes, _printed = prolog(harness.run_guard(env))
        self.assertEqual(harness.log_lines(self.site.log), [])
        self.assertNotIn("LD_PRELOAD", changes)
        self.assertIn("unsafe-module-name", changes["EESSI_ARCH_GUARD_FAILED"])

    def test_no_single_eessi_module_does_not_restore(self):
        env = self.env(LOADEDMODULES="R/4.4.2")
        changes, _printed = prolog(harness.run_guard(env))
        self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->restore-failed"))
        self.assertEqual(harness.log_lines(self.site.log), [])

    def test_archdetect_error_is_loud(self):
        self.site.write_cpuinfo(fail=True)
        env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        changes, printed = prolog(harness.run_guard(env))
        self.assertIn("archdetect-failed", changes["EESSI_ARCH_GUARD_FAILED"])
        self.assertIn("environment left as it was", printed[0])

    def test_missing_lmod_is_loud_on_a_mismatch(self):
        env = self.env()
        del env["LMOD_CMD"]
        changes, _printed = prolog(harness.run_guard(env))
        self.assertIn("lmod-unavailable", changes["EESSI_ARCH_GUARD_FAILED"])


class AcceleratorTests(PolicyCase):
    def gpu_env(self, accel, gpu, cc):
        self.site.add_accel(ZEN4, CC90)
        self.site.add_accel(ZEN4, CC120)
        self.site.add_module(ZEN4, "R/4.4.2", accel=CC120)
        self.site.write_cpuinfo("zen4")
        return harness.base_env(self.site, ZEN4, ["R/4.4.2"], accel=accel, gpu=gpu, gpu_cc=cc)

    def test_accel_difference_is_part_of_the_label(self):
        env = self.gpu_env(CC90, "1", "12.0")
        changes, _printed = prolog(harness.run_guard(env))
        reloaded = changes["EESSI_ARCH_GUARD_RELOADED"]
        self.assertIn(CC90, reloaded)
        self.assertIn(CC120, reloaded)

    def test_accel_override_skips_the_accel_probe(self):
        env = self.gpu_env(CC90, "1", "12.0")
        env["EESSI_ACCELERATOR_TARGET_OVERRIDE"] = CC90
        proc = harness.run_guard(env)
        prolog(proc)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(harness.log_lines(self.site.arch_log), ["cpupath"])
        self.assertIs(self.dry(env)["compare_accel"], False)


class StopTests(PolicyCase):
    def hang_env(self, **extra):
        env = self.env(
            node="zen4",
            LOADEDMODULES="EESSI/2025.06:R/4.4.2",
            EESSI_TEST_GPU="1",
            EESSI_TEST_GPU_HANG="30",
            EESSI_TEST_HANG_PID=os.path.join(self.site.root, "hang.pid"),
            **extra,
        )
        return env

    def hung_pid(self):
        path = Path(self.site.root) / "hang.pid"
        for _ in range(200):
            if path.is_file() and path.read_text(encoding="ascii").strip():
                return int(path.read_text(encoding="ascii"))
            time.sleep(0.05)
        self.fail("the hung child never started")

    def test_the_time_limit_ends_the_decision(self):
        env = self.hang_env()
        started = time.time()
        proc = harness.run_guard(env, ["--timeout", "0.4"])
        self.assertLess(time.time() - started, 5)
        changes, printed = prolog(proc)
        self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->timeout"))
        self.assertIn("time limit", printed[0])

    def stop_with(self, sig, args=()):
        env = self.hang_env()
        child = subprocess.Popen(
            [*harness.GUARD_ARGV, *args],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        pid = self.hung_pid()
        child.send_signal(sig)
        out, err = child.communicate(timeout=20)
        for _ in range(100):
            if not pid_alive(pid):
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("the child of the decision outlived the guard")
        return child.returncode, out, err

    def test_sigterm_ends_the_decision_and_kills_the_child(self):
        code, out, _err = self.stop_with(signal.SIGTERM)
        self.assertEqual(code, 0)
        changes, printed = apply_task_prolog(out)
        self.assertTrue(changes["EESSI_ARCH_GUARD_FAILED"].endswith("->terminated"))
        self.assertTrue(printed)

    def test_sigint_is_a_skip_and_kills_the_child(self):
        code, out, _err = self.stop_with(signal.SIGINT)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("print eessi-arch-guard: skipped"), out)
        self.assertNotIn("EESSI_ARCH_GUARD_FAILED", out)

    def test_an_error_outside_the_decision_is_a_skip_and_exits_zero(self):
        if os.geteuid() == 0:
            self.skipTest("root opens a directory of mode 000")
        env = self.hang_env(SLURM_JOB_ID="6061", SLURM_STEP_ID="batch")
        cache_dir = os.path.join(self.site.root, "cache")
        os.mkdir(cache_dir, 0)
        env["TMPDIR"] = cache_dir
        try:
            proc = harness.run_guard(env, ["--timeout", "1"])
        finally:
            os.chmod(cache_dir, 0o700)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertTrue(proc.stdout.startswith("print eessi-arch-guard: skipped, "), proc.stdout)
        self.assertEqual(proc.stdout.count("\n"), 1)

    def test_unwritable_stdout_still_exits_zero(self):
        base = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LC_ALL": "C"}
        for args in (["--version"], ["--help"]):
            with open("/dev/full", "wb") as sink:
                proc = subprocess.run(
                    [*harness.GUARD_ARGV, *args],
                    stdout=sink,
                    stderr=subprocess.PIPE,
                    env=base,
                    check=False,
                )
            self.assertEqual(proc.returncode, 0, (args, proc.stderr))
            read_fd, write_fd = os.pipe()
            os.close(read_fd)
            proc = subprocess.run(
                [*harness.GUARD_ARGV, *args],
                stdout=write_fd,
                stderr=subprocess.PIPE,
                env=base,
                check=False,
            )
            os.close(write_fd)
            self.assertEqual(proc.returncode, 0, (args, proc.stderr))


class TimeoutOptionTests(PolicyCase):
    def test_timeout_option_values(self):
        env = self.env()
        for text, shown in (("4", 4.0), ("1.5", 1.5), ("2147483.647", 2147483.647)):
            with self.subTest(timeout=text):
                self.assertEqual(self.dry(env, ["--timeout", text])["timeout"], shown)
        self.assertEqual(self.dry(env, ["--timeout=8"])["timeout"], 8.0)
        self.assertEqual(self.dry(env)["timeout"], 60.0)

    def test_a_bad_timeout_falls_back_to_the_default_with_a_warning(self):
        env = self.env()
        for text in ("0", "-1", "nope", "2147483.648", "4294967.294", "9223372036", ""):
            with self.subTest(timeout=text):
                proc = harness.run_guard(env, ["--dry-run", "--json", "--timeout", text])
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(proc.stdout)["timeout"], 60.0)
                self.assertIn("--timeout is not a positive number of seconds", proc.stderr)


class CpuInfoFileTests(PolicyCase):
    """A cpuinfo file that is not a regular file has no flags and cannot block the guard."""

    CHAIN = "x86_64/intel/sapphirerapids:x86_64/intel/icelake:x86_64/generic"

    def setUp(self):
        super().setUp()
        for cpu in ("x86_64/intel/sapphirerapids", "x86_64/intel/icelake"):
            self.site.add_cpu(cpu)
        script = Path(self.site.init) / "eessi_archdetect.sh"
        script.write_text(
            '#!/bin/sh\ncase "$*" in\n*cpupath*) echo %s ;;\n*) exit 2 ;;\nesac\n' % self.CHAIN,
            encoding="utf-8",
        )
        script.chmod(0o755)

    def target(self, path):
        env = harness.base_env(self.site, ZEN4, [], gpu="none")
        env["EESSI_PROC_CPUINFO"] = str(path)
        proc = harness.run_with_timeout(
            [*harness.GUARD_ARGV, "--dry-run", "--json"], env=env, timeout=5
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        return json.loads(proc.stdout)["now"]

    def test_the_waitpkg_flag_decides_between_the_candidates(self):
        regular = Path(self.site.root) / "cpuinfo.flags"
        regular.write_text("flags : avx2 fma waitpkg\n", encoding="ascii")
        self.assertEqual(self.target(regular), "x86_64/intel/sapphirerapids")
        regular.write_text("flags : avx2 fma\n", encoding="ascii")
        self.assertEqual(self.target(regular), "x86_64/intel/icelake")

    def test_a_fifo_or_directory_gives_no_flags(self):
        fifo = Path(self.site.root) / "cpu.fifo"
        os.mkfifo(fifo, 0o600)
        directory = Path(self.site.root) / "cpu.dir"
        directory.mkdir()
        for path in (fifo, directory):
            with self.subTest(path=path.name):
                self.assertEqual(self.target(path), "x86_64/intel/icelake")


class ConfigLookupTests(PolicyCase):
    def test_the_config_is_looked_up_beside_the_invoked_command(self):
        bindir = os.path.join(self.site.root, "invoked")
        os.mkdir(bindir)
        program = os.path.join(bindir, "eessi-arch-guard")
        with open(harness.PROGRAM, "rb") as src, open(program, "wb") as dst:
            dst.write(src.read())
        os.chmod(program, 0o755)
        conf = os.path.join(bindir, "eessi-arch-guard.conf")
        with open(conf, "w", encoding="utf-8") as handle:
            handle.write("timeout = 5\n")
        env = {"PATH": bindir + ":/usr/bin:/bin", "LC_ALL": "C"}
        proc = subprocess.run(
            ["eessi-arch-guard", "--dry-run", "--json"],
            capture_output=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["config"]["path"], conf)
        self.assertIn("not owned by root", report["config"]["warning"])


class ByteTests(PolicyCase):
    def test_a_byte_that_is_not_utf8_survives_the_restore(self):
        env = self.env(LANG="C", LC_ALL="C")
        raw = "/opt/caf\udce9/bin"
        env["PATH"] = raw + ":" + env["PATH"]
        env["EESSI_TEST_BASE_PATH"] = raw + ":" + env["EESSI_TEST_BASE_PATH"]
        proc = subprocess.run(list(harness.GUARD_ARGV), capture_output=True, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"/opt/caf\xe9/bin", proc.stdout)
        self.assertNotIn(b"skipped", proc.stdout)
        self.assertIn(("EESSI_SOFTWARE_SUBDIR=%s" % ZEN2).encode("ascii"), proc.stdout)

    def test_the_fake_lmod_writes_environment_bytes(self):
        env = dict(os.environ)
        env["PATH"] = "/usr/bin:/bin"
        env["EESSI_TEST_BASE_PATH"] = "/opt/caf\udce9/bin"
        proc = subprocess.run(
            ["/usr/bin/python3", "-I", str(harness.FAKE_LMOD), "bash", "--force", "purge"],
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"export PATH='/opt/caf\xe9/bin'", proc.stdout)


class HostileModuleTests(PolicyCase):
    def test_hostile_module_names_are_refused_before_lmod(self):
        import random

        rng = random.Random(20261101)
        hostile = ["\n", "\r", "'", '"', "$", "`", "$(touch /tmp/x)", " ", ";", "|", "&", "\\"]
        body = "R/4.4.2"
        base = self.env()
        for _ in range(40):
            cut = rng.randrange(len(body) + 1)
            name = body[:cut] + rng.choice(hostile) + body[cut:]
            env = dict(base)
            env["LOADEDMODULES"] = "EESSI/2025.06:" + name
            proc = harness.run_guard(env)
            changes, _printed = prolog(proc)
            self.assertIn("unsafe-module-name", changes["EESSI_ARCH_GUARD_FAILED"], repr(name))
            self.assertNotIn("EESSI_SOFTWARE_SUBDIR", changes)
        self.assertEqual(harness.log_lines(self.site.log), [])


if __name__ == "__main__":
    unittest.main()
