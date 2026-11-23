"""The Lmod site package warns on a subdirectory mismatch and can track loads."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

import private_tmp  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
LUA = shutil.which("lua")


@unittest.skipUnless(LUA, "lua is not installed")
class SitePackageTests(unittest.TestCase):
    def test_a_mismatch_warns_and_a_match_does_not(self):
        proc = subprocess.run(
            ["lua", str(ROOT / "tests" / "site_package_check.lua"), str(ROOT)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("ok", proc.stdout)

    def test_usage_tracking_logger_fallback_emits_eb_keep_logs_record(self):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="eessi-arch-guard-sp-") as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            captured = root / "logger.log"
            logger_stub = bindir / "logger"
            logger_stub.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" > ' + f'"{captured}"\n',
                encoding="utf-8",
            )
            logger_stub.chmod(0o755)

            lua_script = root / "run.lua"
            mod_path = (
                "/cvmfs/software.eessi.io/versions/2025.06/software/linux/"
                "x86_64/amd/zen4/modules/all/GROMACS/2024.1.lua"
            )
            lua_script.write_text(
                "\n".join(
                    [
                        f'local sp = dofile("{ROOT / "share" / "lmod" / "SitePackage.lua"}")',
                        "sp.find_posix_syslog = function() return nil end",
                        f'sp.load_hook({{ fn = "{mod_path}", modFullName = "GROMACS/2024.1" }})',
                        "local count = sp.flush_usage_records()",
                        'assert(count == 1, "expected one flushed record")',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            env = {
                "PATH": f"{bindir}:/usr/bin:/bin",
                "HOME": "/tmp",
                "USER": "alice",
                "HOSTNAME": "node01",
                "SLURM_JOB_ID": "71234",
                "EESSI_USAGE_TRACKING": "1",
            }
            proc = subprocess.run(
                [LUA, str(lua_script)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertTrue(captured.is_file(), "stub logger was not invoked")
            line = captured.read_text(encoding="utf-8").strip()
            prefix = "-t ModuleUsageTracking -p local0.info -- "
            self.assertTrue(line.startswith(prefix), line)
            payload = line[len(prefix) :]
            fields = dict(token.split("=", 1) for token in payload.split())
            self.assertEqual(
                set(fields),
                {"user", "jobid", "module", "path", "host", "time"},
            )
            self.assertEqual(fields["user"], "alice")
            self.assertEqual(fields["jobid"], "71234")
            self.assertEqual(fields["module"], "GROMACS/2024.1")
            self.assertEqual(fields["path"], mod_path)
            self.assertTrue(fields["host"])
            self.assertRegex(fields["time"], r"^\d+(?:\.\d+)?$")
            self.assertGreaterEqual(float(fields["time"]), 0.0)

    @unittest.skipUnless(
        Path("/usr/share/lmod/lmod/libexec/lmod").is_file(),
        "Lmod libexec is not installed",
    )
    def test_real_lmod_load_with_usage_tracking(self):
        import re
        import tempfile

        lmod = Path("/usr/share/lmod/lmod/libexec/lmod")

        with tempfile.TemporaryDirectory(prefix="eessi-arch-guard-lmod-track-") as tmp:
            root = Path(tmp)
            moddir = root / "modules" / "Demo"
            moddir.mkdir(parents=True)
            (moddir / "1.0.lua").write_text('setenv("DEMO_LOADED", "1")\n', encoding="utf-8")

            pkgdir = root / "lmod_pkg"
            pkgdir.mkdir()
            shutil.copy(ROOT / "share" / "lmod" / "SitePackage.lua", pkgdir / "SitePackage.lua")

            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",
                "USER": "alice",
                "LANG": "C",
                "LC_ALL": "C",
                "MODULEPATH": str(root / "modules"),
                "EESSI_USAGE_TRACKING": "1",
                "SLURM_JOB_ID": "8899",
                "LMOD_PACKAGE_PATH": str(pkgdir),
            }
            proc = subprocess.run(
                [str(lmod), "bash", "load", "Demo/1.0"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(proc.stderr, "")
            self.assertIn("DEMO_LOADED", proc.stdout)
            self.assertNotIn("ModuleUsageTracking", proc.stdout)
            self.assertTrue(re.search(r"export\s+DEMO_LOADED", proc.stdout), proc.stdout)

    def test_path_parts_handles_duplicate_software_prefix_and_repeated_slashes(self):
        sp_path = ROOT / "share" / "lmod" / "SitePackage.lua"
        script = "\n".join(
            [
                f'local sp = dofile("{sp_path}")',
                "local cpu1, accel1 = sp.path_parts("
                '"/tmp/software/linux/mirror/software/linux/x86_64/amd/zen4/'
                'modules/all/Foo/1.0.lua")',
                'assert(cpu1 == "x86_64/amd/zen4", "cpu1=" .. tostring(cpu1))',
                'assert(accel1 == nil, "accel1=" .. tostring(accel1))',
                "local cpu2, accel2 = sp.path_parts("
                '"/cvmfs/software.eessi.io/versions/2026.06/software/linux//'
                'x86_64/amd/zen4/accel/nvidia/cc90//modules/all/CUDA/12.6.0.lua")',
                'assert(cpu2 == "x86_64/amd/zen4", "cpu2=" .. tostring(cpu2))',
                'assert(accel2 == "accel/nvidia/cc90", "accel2=" .. tostring(accel2))',
            ]
        )
        proc = subprocess.run(
            [LUA, "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_blank_and_control_module_names_fall_back_to_path_and_sanitize_warnings(self):
        sp_path = ROOT / "share" / "lmod" / "SitePackage.lua"
        zen3 = (
            "/cvmfs/software.eessi.io/versions/2026.06/software/linux/"
            "x86_64/amd/zen3/modules/all/Foo/1.0.lua"
        )
        cc90 = (
            "/cvmfs/software.eessi.io/versions/2026.06/software/linux/"
            "x86_64/amd/zen4/accel/nvidia/cc90/modules/all/CUDA/12.6.0.lua"
        )
        script = "\n".join(
            [
                f'local sp = dofile("{sp_path}")',
                f'local m1 = sp.warn_mismatch("{zen3}", "x86_64/amd/zen4", "   ")',
                f'assert(m1 ~= nil and m1:find("{zen3}", 1, true), tostring(m1))',
                f'assert(sp.warn_mismatch("{zen3}", "   ", "Foo/1.0") == nil)',
                f'local m2 = sp.warn_mismatch("{zen3}", "x86_64/amd/zen4", "Foo\\n1.0")',
                'assert(m2 ~= nil and m2:sub(1, #m2 - 1):find("\\n", 1, true) == nil, '
                'tostring(m2))',
                f'local a1 = sp.warn_accel("{cc90}", "accel/nvidia/cc80", "\\x01\\x02")',
                f'assert(a1 ~= nil and a1:find("{cc90}", 1, true), tostring(a1))',
            ]
        )
        proc = subprocess.run(
            [LUA, "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_usage_record_rejects_control_only_fields_and_trims_edges(self):
        sp_path = ROOT / "share" / "lmod" / "SitePackage.lua"
        script = "\n".join(
            [
                f'local sp = dofile("{sp_path}")',
                'assert(sp.format_usage_record({ modFullName = "\\x01\\x02", fn = "/a/1.lua" })'
                " == nil)",
                'assert(sp.format_usage_record({ modFullName = "A/1.0", fn = "\\x7f" }) == nil)',
                'local rec = sp.format_usage_record({ modFullName = "  A/1.0\\n", '
                'fn = "\\t/opt/site/a b/1.0.lua " })',
                'assert(rec ~= nil and rec:find("module=A/1.0 ", 1, true), tostring(rec))',
                'assert(rec:find("path=/opt/site/a_b/1.0.lua ", 1, true), tostring(rec))',
                'assert(rec:find("user=unknown ", 1, true), tostring(rec))',
                'assert(rec:find("jobid=nil ", 1, true), tostring(rec))',
            ]
        )
        proc = subprocess.run(
            [LUA, "-e", script],
            env={
                "PATH": "/usr/bin:/bin",
                "USER": "\x01",
                "HOSTNAME": "node01",
                "SLURM_JOB_ID": "\x1b",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_missing_logger_on_path_returns_false_from_emit_syslog_and_zero_flushed(self):
        sp_path = ROOT / "share" / "lmod" / "SitePackage.lua"
        mod_path = (
            "/cvmfs/software.eessi.io/versions/2026.06/software/linux/"
            "x86_64/amd/zen4/modules/all/GROMACS/2024.1.lua"
        )
        script = "\n".join(
            [
                f'local sp = dofile("{sp_path}")',
                "sp.find_posix_syslog = function() return nil end",
                'assert(sp.emit_syslog({ "user=alice module=A/1 path=/a host=h time=1.0" })'
                " == false)",
                f'sp.load_hook({{ fn = "{mod_path}", modFullName = "GROMACS/2024.1" }})',
                "assert(sp.flush_usage_records() == 0)",
                "assert(#sp.usage_records == 0)",
            ]
        )
        proc = subprocess.run(
            [LUA, "-e", script],
            env={
                "PATH": "/nonexistent",
                "USER": "alice",
                "HOSTNAME": "node01",
                "EESSI_USAGE_TRACKING": "1",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
