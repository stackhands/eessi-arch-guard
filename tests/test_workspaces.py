"""Private configured restore workspaces for strict job guards.

The restore helper runs the module commands in a directory it makes under a
parent it picks. A wrapper in place of Lmod records where that directory is,
what the helper sets in its environment, and the command line of the helper.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402

ZEN2 = "x86_64/amd/zen2"
ZEN4 = "x86_64/amd/zen4"

WRAPPER = """#!/bin/sh
{
  p=$$
  while [ "$p" -gt 1 ]; do
    line=$(tr '\\0' ' ' < /proc/$p/cmdline)
    echo "ARGV=$line"
    case "$line" in
    *--restore*)
      tr '\\0' '\\n' < /proc/$p/environ | sed -n 's/^\\(HOME\\|TMPDIR\\|EESSI_ARCH_GUARD_WORKDIR\\)=/H_\\1=/p'
      w=$(tr '\\0' '\\n' < /proc/$p/environ | sed -n 's/^EESSI_ARCH_GUARD_WORKDIR=//p')
      if [ -d "$w" ]; then echo "ISDIR=yes"; fi
      ;;
    esac
    p=$(sed 's/.*) [^ ]* //; s/ .*//' /proc/$p/stat)
  done
} >> "%s"
exec /usr/bin/python3 -I %s "$@"
"""


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.site = harness.Site()
        self.addCleanup(self.site.close)
        for cpu in (ZEN2, ZEN4):
            self.site.add_cpu(cpu)
            self.site.add_module(cpu, "R/4.4.2")
        self.site.write_cpuinfo("zen2")
        self.root = tempfile.mkdtemp(prefix="eag-workspace-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.chmod(self.root, 0o700)
        self.record = os.path.join(self.root, "..record-" + os.path.basename(self.root))
        wrapper = os.path.join(self.site.root, "lmod-record")
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write(WRAPPER % (self.record, harness.FAKE_LMOD))
        os.chmod(wrapper, 0o755)
        self.addCleanup(lambda: os.path.exists(self.record) and os.unlink(self.record))
        self.env = harness.base_env(self.site, ZEN4, ["R/4.4.2"], gpu="none")
        self.env["LMOD_CMD"] = wrapper
        self.env["HOME"] = self.root
        self.env["TMPDIR"] = self.root

    def recorded(self):
        found = {}
        if os.path.exists(self.record):
            with open(self.record, encoding="utf-8") as handle:
                for line in handle:
                    key, _sep, value = line.rstrip("\n").partition("=")
                    found.setdefault(key, []).append(value)
        return found

    def failed_with(self, proc, text):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("->restore-failed", proc.stdout)
        self.assertIn(text, proc.stdout)
        self.assertEqual(self.recorded(), {})

    def test_strict_restore_honors_existing_private_tmpdir(self):
        self.env["SLURM_JOB_ID"] = "9191"
        proc = harness.run_guard(self.env, ["--strict"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("EESSI_ARCH_GUARD_RELOADED", proc.stdout)
        report = json.loads(harness.run_guard(self.env, ["--strict", "--dry-run", "--json"]).stdout)
        self.assertEqual(os.path.dirname(report["cache"]["path"]), os.path.realpath(self.root))
        self.assertTrue(os.path.exists(report["cache"]["path"]))
        work = self.recorded()["H_EESSI_ARCH_GUARD_WORKDIR"][0]
        self.assertEqual(os.path.dirname(work), os.path.realpath(self.root))

    def test_explicit_workspace_has_priority(self):
        other = tempfile.mkdtemp(prefix="eag-workspace-other-")
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        self.env["EESSI_ARCH_GUARD_WORKDIR"] = self.root
        self.env["TMPDIR"] = other
        proc = harness.run_guard(self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("EESSI_ARCH_GUARD_RELOADED", proc.stdout)
        work = self.recorded()["H_EESSI_ARCH_GUARD_WORKDIR"][0]
        self.assertEqual(os.path.dirname(work), os.path.realpath(self.root))

    def test_untrusted_explicit_workspace_is_refused_without_creation(self):
        missing = os.path.join(self.root, "missing")
        self.env["EESSI_ARCH_GUARD_WORKDIR"] = missing
        self.failed_with(harness.run_guard(self.env), "restore workspace unavailable")
        self.assertFalse(os.path.exists(missing))
        public = os.path.join(self.root, "public")
        os.mkdir(public, 0o755)
        os.chmod(public, 0o755)
        self.env["EESSI_ARCH_GUARD_WORKDIR"] = public
        self.failed_with(harness.run_guard(self.env), "restore workspace unavailable")

    def test_explicit_symlink_is_refused(self):
        link = os.path.join(self.root, "alias")
        os.symlink(self.root, link, target_is_directory=True)
        self.env["EESSI_ARCH_GUARD_WORKDIR"] = link
        self.failed_with(harness.run_guard(self.env), "restore workspace unavailable")

    def test_existing_state_workspace_handles_missing_job_tmpdir(self):
        state = os.path.join(self.root, "state")
        work = os.path.join(state, "eessi-arch-guard")
        os.makedirs(work, mode=0o700)
        self.env["TMPDIR"] = os.path.join(self.root, "not-created")
        self.env["XDG_STATE_HOME"] = state
        proc = harness.run_guard(self.env, ["--strict"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("EESSI_ARCH_GUARD_RELOADED", proc.stdout)
        self.assertEqual(os.path.dirname(self.recorded()["H_EESSI_ARCH_GUARD_WORKDIR"][0]), work)
        self.assertFalse(os.path.exists(self.env["TMPDIR"]))

    def test_strict_mode_without_private_workspace_has_no_cache_fallback(self):
        self.env["TMPDIR"] = os.path.join(self.root, "missing")
        self.env["SLURM_JOB_ID"] = "9292"
        proc = harness.run_guard(self.env, ["--strict"])
        self.assertEqual(proc.returncode, 78, proc.stderr)
        self.assertIn("strict mode needs an existing private job or state workspace", proc.stdout)
        self.assertEqual(self.recorded(), {})
        self.assertFalse(os.path.exists(os.path.join(self.root, ".local")))
        report = json.loads(harness.run_guard(self.env, ["--strict", "--dry-run", "--json"]).stdout)
        self.assertIsNone(report["cache"]["path"])
        leftovers = [n for n in os.listdir(self.root) if n.startswith(".eessi-arch-guard")]
        self.assertEqual(leftovers, [])

    def test_restore_helper_and_its_cleanup_stay_under_selected_parent(self):
        proc = harness.run_guard(self.env, ["--strict"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        found = self.recorded()
        work = found["H_EESSI_ARCH_GUARD_WORKDIR"][0]
        self.assertEqual(os.path.dirname(work), os.path.realpath(self.root))
        self.assertEqual(found["ISDIR"][0], "yes")
        self.assertEqual(found["H_HOME"][0], work)
        self.assertEqual(found["H_TMPDIR"][0], work)
        helper = [line for line in found["ARGV"] if "--restore" in line]
        self.assertTrue(helper, found["ARGV"])
        self.assertIn("--strict", helper[0])
        self.assertFalse(os.path.exists(work))
        self.assertTrue(os.path.isdir(self.root))


class RestoreMismatchTests(unittest.TestCase):
    def test_restore_mismatch_refusals_across_cpu_accel_eessi_and_app_modules(self):
        zen2 = "x86_64/amd/zen2"
        zen4 = "x86_64/amd/zen4"
        cc80 = "accel/nvidia/cc80"
        cases = (
            (
                "cpu-target",
                "none",
                "",
                "echo 'export EESSI_SOFTWARE_SUBDIR=x86_64/amd/zen4'\n"
                "echo 'export LOADEDMODULES=EESSI/2025.06:R/4.4.2'\n",
                "reload did not select this node's CPU target",
            ),
            (
                "accel-target",
                "1",
                "9.0",
                "echo 'export EESSI_SOFTWARE_SUBDIR=x86_64/amd/zen2'\n"
                f"echo 'export EESSI_ACCEL_SUBDIR={cc80}'\n"
                "echo 'export LOADEDMODULES=EESSI/2025.06:R/4.4.2'\n",
                "reload did not select this node's accelerator target",
            ),
            (
                "eessi-module",
                "none",
                "",
                "echo 'export EESSI_SOFTWARE_SUBDIR=x86_64/amd/zen2'\n"
                "echo 'export EESSI_ACCEL_SUBDIR='\n"
                "echo 'export LOADEDMODULES=R/4.4.2'\n",
                "reload did not load the EESSI module",
            ),
            (
                "app-module",
                "none",
                "",
                "echo 'export EESSI_SOFTWARE_SUBDIR=x86_64/amd/zen2'\n"
                "echo 'export EESSI_ACCEL_SUBDIR='\n"
                "echo 'export LOADEDMODULES=EESSI/2025.06'\n",
                "reload did not load R/4.4.2",
            ),
        )
        for name, gpu, cc, lmod_script, expected_reason in cases:
            with self.subTest(case=name):
                site = harness.Site()
                try:
                    site.add_cpu(zen2)
                    site.add_cpu(zen4)
                    site.add_module(zen4, "R/4.4.2")
                    site.add_module(zen2, "R/4.4.2")
                    site.write_cpuinfo("zen2")
                    lmod_path = Path(site.root) / f"lmod-{name}"
                    lmod_path.write_text("#!/bin/sh\n" + lmod_script, encoding="utf-8")
                    lmod_path.chmod(0o755)
                    env = harness.base_env(site, zen4, ["R/4.4.2"], gpu=gpu, gpu_cc=cc)
                    env["LMOD_CMD"] = str(lmod_path)
                    proc = harness.run_guard(env)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stderr, "")
                    self.assertIn("->restore-failed", proc.stdout)
                    self.assertIn(expected_reason, proc.stdout)
                    self.assertIn("environment left as it was", proc.stdout)
                finally:
                    site.close()
